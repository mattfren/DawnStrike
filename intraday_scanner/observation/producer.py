"""Bind existing authenticated capture artifacts into the R2 sidecar inputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .contracts import SCOPES, canonical_json, parse_utc, sha256_json
from .runner import validate_output_root


class ObservationProducerError(ValueError):
    """Existing capture evidence cannot be safely bound to R2 inputs."""


def _read_json(path: Path, *, label: str, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ObservationProducerError(f"{label} is not a regular file: {path}")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ObservationProducerError(f"{label} exceeds its byte bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObservationProducerError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ObservationProducerError(f"{label} must be an object")
    return value


def _receipt_hash(receipt: dict[str, Any]) -> str:
    declared = str(receipt.get("receipt_hash_sha256") or "")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    actual = hashlib.sha256(canonical_json(unsigned)).hexdigest()
    if declared != actual:
        raise ObservationProducerError("capture receipt hash mismatch")
    return declared


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    os.replace(temp, path)


def _page_items(state: dict[str, Any], *, root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for symbol, endpoints in (state.get("symbols") or {}).items():
        if not isinstance(endpoints, dict):
            continue
        for endpoint, endpoint_state in endpoints.items():
            if not isinstance(endpoint_state, dict):
                continue
            if endpoint not in {"bars", "trades", "quotes", "corporate_actions"}:
                continue
            for page in endpoint_state.get("pages", []):
                if not isinstance(page, dict):
                    raise ObservationProducerError("capture state page is invalid")
                page_path = Path(str(page.get("page_path") or ""))
                if not page_path.is_absolute():
                    page_path = root / page_path
                page_value = _read_json(page_path, label="capture page")
                if page_value.get("raw_payload_hash_sha256") != page.get(
                    "raw_payload_hash_sha256"
                ):
                    raise ObservationProducerError("capture page hash identity changed")
                raw_items = page_value.get("items")
                if not isinstance(raw_items, list):
                    raise ObservationProducerError("capture page items are missing")
                for item in raw_items:
                    if isinstance(item, dict):
                        items.append(
                            {
                                "symbol": str(symbol).upper(),
                                "endpoint": endpoint,
                                "item": item,
                                "source_artifact_hash_sha256": str(
                                    page.get("raw_artifact_hash_sha256")
                                    or page.get("raw_payload_hash_sha256")
                                    or ""
                                ),
                            }
                        )
    return items


def build_observation_inputs(
    *,
    capture_receipt_path: str | Path,
    scope_declaration_path: str | Path,
    output_root: str | Path,
    decision_deadline: str,
    repository_root: str | Path | None = None,
    max_events: int = 100_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """Create a bound two-scope manifest and raw event stream from capture evidence."""

    if not 1 <= max_events <= 100_000 or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ObservationProducerError("producer bounds are outside the supported range")
    root = validate_output_root(output_root, repository_root=repository_root)
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = Path(capture_receipt_path).resolve()
    declaration_path = Path(scope_declaration_path).resolve()
    receipt = _read_json(receipt_path, label="capture receipt")
    declaration = _read_json(declaration_path, label="scope declaration")
    receipt_hash = _receipt_hash(receipt)
    session_id = str(receipt.get("session_id") or "").strip()
    market_date = str(receipt.get("market_date") or "").strip()
    source_config_hash = str(receipt.get("source_config_hash") or "").strip()
    provider = str(receipt.get("provider") or "").strip()
    feed = str(receipt.get("feed") or "").strip()
    code_sha = str(receipt.get("code_sha") or "").strip()
    if not session_id or not market_date or len(source_config_hash) != 64:
        raise ObservationProducerError("capture receipt lacks session/config identity")
    if not provider or not feed or not code_sha:
        raise ObservationProducerError("capture receipt lacks provider/code lineage")
    if (
        receipt.get("research_only") is not True
        or receipt.get("broker_execution_enabled") is not False
    ):
        raise ObservationProducerError("capture receipt is not research-only and broker-disabled")
    deadline = parse_utc(decision_deadline, label="decision_deadline")
    scopes = declaration.get("scopes")
    if declaration.get("schema_version") != "dawnstrike.observation.scope_declaration.v1":
        raise ObservationProducerError("unsupported scope declaration schema")
    if not isinstance(scopes, dict) or set(scopes) != set(SCOPES):
        raise ObservationProducerError("scope declaration must contain exactly two scopes")
    entries: dict[str, list[dict[str, Any]]] = {}
    symbols: set[str] = set()
    for scope in SCOPES:
        if not isinstance(scopes[scope], list):
            raise ObservationProducerError(f"scope {scope} is not a list")
        entries[scope] = []
        for row in scopes[scope]:
            if not isinstance(row, dict):
                raise ObservationProducerError("scope row is not an object")
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol or symbol in symbols:
                raise ObservationProducerError("scope declaration has duplicate/blank symbol")
            symbols.add(symbol)
            entries[scope].append({**row, "symbol": symbol})
    state_path = Path(str(receipt.get("state_path") or "")).resolve()
    state = _read_json(state_path, label="capture state")
    captured_symbols = {str(symbol).upper() for symbol in (receipt.get("symbols") or [])}
    if not captured_symbols.issubset(symbols):
        raise ObservationProducerError("capture symbols are absent from the two-scope declaration")
    page_items = _page_items(state, root=state_path.parent)
    completed_at = parse_utc(str(receipt.get("completed_at") or ""), label="completed_at")
    raw_events: list[dict[str, Any]] = []
    for row in page_items:
        item = row["item"]
        event_raw = item.get("timestamp") or item.get("t") or item.get("time")
        if not event_raw:
            continue
        event_at = parse_utc(str(event_raw), label="provider event timestamp")
        raw_events.append(
            {
                "session_id": session_id,
                "scope": next(scope for scope in SCOPES if row["symbol"] in {
                    entry["symbol"] for entry in entries[scope]
                }),
                "symbol": row["symbol"],
                "source": f"{provider}:{feed}:{row['endpoint']}",
                "provider": provider,
                "feed": feed,
                "capture_receipt_sha256": receipt_hash,
                "source_config_sha256": source_config_hash,
                "code_sha": code_sha,
                "event_time": event_at.isoformat(),
                "available_at": completed_at.isoformat(),
                "kind": row["endpoint"],
                "payload": item,
                "source_artifact_hash_sha256": row["source_artifact_hash_sha256"],
            }
        )
    raw_bytes = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for row in raw_events
    )
    if len(raw_events) > max_events or len(raw_bytes) > max_bytes:
        raise ObservationProducerError("producer output exceeds bounded event limits")
    expectation_status = "COMPLETE" if receipt.get("status") == "COMPLETE" else "INCOMPLETE"
    manifest = {
        "schema_version": "dawnstrike.observation.universe.v1",
        "session_id": session_id,
        "market_date": market_date,
        "decision_deadline": deadline.isoformat(),
        "universe_generation_id": str(
            declaration.get("universe_generation_id") or sha256_json(scopes)
        ),
        "source_config_sha256": source_config_hash,
        "collection_expectation": {
            "status": expectation_status,
            "expected_entries": len(symbols),
            "receipt_id": str(receipt.get("run_id") or receipt_hash),
        },
        "source_lineage": {
            "provider": provider,
            "feed": feed,
            "capture_receipt_sha256": receipt_hash,
            "source_config_sha256": source_config_hash,
            "session_id": session_id,
            "code_sha": code_sha,
            "capture_status": receipt.get("status"),
            "request_start": receipt.get("request_start"),
            "request_end": receipt.get("request_end"),
            "completed_at": receipt.get("completed_at"),
        },
        "scopes": scopes,
    }
    manifest_path = root / "universe-manifest.json"
    events_path = root / "raw-events.jsonl"
    _atomic_json(manifest_path, manifest)
    events_path.write_bytes(raw_bytes)
    producer_receipt = {
        "schema_version": "dawnstrike.observation.producer_receipt.v1",
        "status": "READY",
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_json(manifest),
        "raw_events_path": str(events_path),
        "raw_events_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "capture_receipt_path": str(receipt_path),
        "capture_receipt_sha256": receipt_hash,
        "source_config_sha256": source_config_hash,
        "provider": provider,
        "feed": feed,
        "session_id": session_id,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    _atomic_json(root / "producer-receipt.json", producer_receipt)
    return producer_receipt
