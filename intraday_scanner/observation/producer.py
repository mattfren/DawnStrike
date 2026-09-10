"""Bind existing authenticated capture artifacts into the R2 sidecar inputs."""

from __future__ import annotations

import hashlib
import heapq
import json
import os
from pathlib import Path
from typing import Any

from intraday_scanner.services.intraday_evidence_capture_service import (
    CaptureContractError,
    _validate_checkpoint_pages,
)

from .contracts import SCOPES, canonical_json, parse_utc, sha256_json
from .runner import validate_output_root


class ObservationProducerError(ValueError):
    """Existing capture evidence cannot be safely bound to R2 inputs."""


WINDOW_CONTRACT_SCHEMA_VERSION = "dawnstrike.provider_window_contract.v1"
DERIVED_WINDOW_SEMANTICS = "half_open_v1"
_PROVIDER_WINDOW_CONTRACTS = {
    ("alpaca", "sip"): {
        "contract_id": "alpaca.stock.historical.v1",
        "request_start": "inclusive",
        "request_end": "inclusive",
        "authority": "https://docs.alpaca.markets/us/reference/stockbars",
    }
}


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


def _iter_page_items(
    state: dict[str, Any],
    *,
    root: Path,
    provider: str,
    feed: str,
    artifacts: list[dict[str, Any]],
):
    for symbol, endpoints in (state.get("symbols") or {}).items():
        if not isinstance(endpoints, dict):
            continue
        for endpoint, endpoint_state in endpoints.items():
            if not isinstance(endpoint_state, dict):
                continue
            if endpoint not in {"bars", "trades", "quotes", "corporate_actions"}:
                continue
            pages = endpoint_state.get("pages", [])
            if not isinstance(pages, list):
                raise ObservationProducerError("capture state pages are invalid")
            try:
                _validate_checkpoint_pages(
                    pages,
                    provider=provider,
                    feed=feed,
                    endpoint=endpoint,
                )
            except (CaptureContractError, OSError, ValueError) as exc:
                raise ObservationProducerError(
                    f"capture checkpoint validation failed for {symbol}/{endpoint}"
                ) from exc
            for page in pages:
                if not isinstance(page, dict):
                    raise ObservationProducerError("capture state page is invalid")
                page_path = Path(str(page.get("page_path") or ""))
                if not page_path.is_absolute():
                    page_path = root / page_path
                try:
                    page_path.resolve().relative_to(root.resolve())
                except ValueError as exc:
                    raise ObservationProducerError(
                        "capture page escapes the authenticated capture root"
                    ) from exc
                page_value = _read_json(page_path, label="capture page")
                if page_value.get("raw_payload_hash_sha256") != page.get("raw_payload_hash_sha256"):
                    raise ObservationProducerError("capture page hash identity changed")
                for key in (
                    "page_number",
                    "cursor_in",
                    "cursor_out",
                    "provider",
                    "feed",
                    "endpoint",
                    "raw_payload_hash_sha256",
                    "raw_artifact_hash_sha256",
                    "previous_page_hash_sha256",
                ):
                    if key in page and key in page_value and page_value.get(key) != page.get(key):
                        raise ObservationProducerError(
                            f"capture page checkpoint field changed: {key}"
                        )
                raw_items = page_value.get("items")
                if not isinstance(raw_items, list):
                    raise ObservationProducerError("capture page items are missing")
                for source_item_index, item in enumerate(raw_items):
                    if isinstance(item, dict):
                        yield {
                            "symbol": str(symbol).upper(),
                            "endpoint": endpoint,
                            "item": item,
                            "source_page_path": str(page_path),
                            "source_item_index": source_item_index,
                            "source_artifact_hash_sha256": str(
                                page.get("raw_artifact_hash_sha256")
                                or page.get("raw_payload_hash_sha256")
                                or ""
                            ),
                        }
            if endpoint_state.get("artifact_manifest_id"):
                artifacts.append(
                    {
                        "artifact_manifest_id": endpoint_state.get("artifact_manifest_id"),
                        "endpoint": endpoint,
                        "normalized_artifact_hash_sha256": endpoint_state.get(
                            "aggregate_normalized_hash"
                        ),
                        "raw_artifact_hash_sha256": endpoint_state.get("aggregate_raw_hash"),
                        "symbol": str(symbol).upper(),
                    }
                )


def _page_items(
    state: dict[str, Any],
    *,
    root: Path,
    provider: str,
    feed: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility wrapper; production binding uses the streaming iterator."""

    artifacts: list[dict[str, Any]] = []
    return list(
        _iter_page_items(
            state,
            root=root,
            provider=provider,
            feed=feed,
            artifacts=artifacts,
        )
    ), artifacts


_STRATIFIED_REFERENCE_PANEL = ("DIA", "IWM", "QQQ", "SPY", "TLT")
_STRATIFIED_BIN_COUNT = 6


def _event_key(*, symbol: str, endpoint: str, event_time: str, item: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "symbol": symbol,
                "endpoint": endpoint,
                "event_time": event_time,
                "payload": item,
            }
        )
    ).hexdigest()


def _selection_key(*, row: dict[str, Any], event_time: str) -> str:
    """Rank by immutable source position, never by payload/outcome fields."""

    return hashlib.sha256(
        canonical_json(
            {
                "symbol": str(row["symbol"]).upper(),
                "endpoint": str(row["endpoint"]),
                "event_time": event_time,
                "source_page_path": str(row.get("source_page_path") or ""),
                "source_item_index": int(row.get("source_item_index", -1)),
            }
        )
    ).hexdigest()


def _stratified_selection(
    *,
    state: dict[str, Any],
    root: Path,
    provider: str,
    feed: str,
    request_start: Any,
    request_end: Any,
    max_events: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Select a fixed per-symbol/per-time-bin derivative without outcomes.

    The retained source is scanned twice.  The first pass counts valid source
    events by symbol and fixed five-minute bin.  The second keeps the lowest
    deterministic event-key ranks in each predeclared allocation.  Payloads
    are never scored or inspected for outcomes; only source identity, symbol,
    endpoint, timestamp, and hash rank participate in selection.
    """

    if max_events < len(_STRATIFIED_REFERENCE_PANEL):
        raise ObservationProducerError("stratified panel requires one boundary slot per symbol")
    ordinary_budget = max_events - len(_STRATIFIED_REFERENCE_PANEL)
    per_symbol_budget = ordinary_budget // len(_STRATIFIED_REFERENCE_PANEL)
    if per_symbol_budget < _STRATIFIED_BIN_COUNT:
        raise ObservationProducerError("stratified panel budget is too small for time bins")
    bin_seconds = (request_end - request_start).total_seconds() / _STRATIFIED_BIN_COUNT
    if bin_seconds <= 0:
        raise ObservationProducerError("stratified panel requires a positive capture window")

    counts: dict[tuple[str, int], int] = {
        (symbol, bin_index): 0
        for symbol in _STRATIFIED_REFERENCE_PANEL
        for bin_index in range(_STRATIFIED_BIN_COUNT)
    }
    boundary_counts = {symbol: 0 for symbol in _STRATIFIED_REFERENCE_PANEL}

    def classify(row: dict[str, Any], *, count: bool) -> tuple[str, int | None, str]:
        symbol = str(row["symbol"]).upper()
        if symbol not in _STRATIFIED_REFERENCE_PANEL:
            raise ObservationProducerError("stratified source contains a non-panel symbol")
        item = row["item"]
        event_raw = item.get("timestamp") or item.get("t") or item.get("time")
        if not event_raw:
            raise ObservationProducerError("stratified source event lacks timestamp")
        event_at = parse_utc(str(event_raw), label="provider event timestamp")
        if event_at < request_start or event_at > request_end:
            raise ObservationProducerError("provider event is outside the capture request window")
        event_time = event_at.isoformat()
        if event_at == request_end:
            if count:
                boundary_counts[symbol] += 1
            return symbol, None, event_time
        bin_index = min(
            _STRATIFIED_BIN_COUNT - 1,
            int((event_at - request_start).total_seconds() // bin_seconds),
        )
        if count:
            counts[(symbol, bin_index)] += 1
        return symbol, bin_index, event_time

    first_artifacts: list[dict[str, Any]] = []
    for row in _iter_page_items(
        state,
        root=root,
        provider=provider,
        feed=feed,
        artifacts=first_artifacts,
    ):
        if str(row.get("symbol") or "").upper() not in _STRATIFIED_REFERENCE_PANEL:
            continue
        classify(row, count=True)

    target_counts: dict[tuple[str, int], int] = {}
    for symbol in _STRATIFIED_REFERENCE_PANEL:
        base, remainder = divmod(per_symbol_budget, _STRATIFIED_BIN_COUNT)
        for bin_index in range(_STRATIFIED_BIN_COUNT):
            target_counts[(symbol, bin_index)] = min(
                counts[(symbol, bin_index)], base + (1 if bin_index < remainder else 0)
            )
    boundary_targets = {
        symbol: min(1, boundary_counts[symbol]) for symbol in _STRATIFIED_REFERENCE_PANEL
    }

    # Each heap retains the largest selected rank at its root so a lower rank
    # replaces it.  The counter makes ties deterministic without comparing
    # payload dictionaries.
    heaps: dict[tuple[str, int | None], list[tuple[int, str, int, dict[str, Any], str]]] = {
        **{
            (symbol, bin_index): []
            for symbol in _STRATIFIED_REFERENCE_PANEL
            for bin_index in range(_STRATIFIED_BIN_COUNT)
        },
        **{(symbol, None): [] for symbol in _STRATIFIED_REFERENCE_PANEL},
    }
    counter = 0
    for row in _iter_page_items(
        state,
        root=root,
        provider=provider,
        feed=feed,
        artifacts=[],
    ):
        if str(row.get("symbol") or "").upper() not in _STRATIFIED_REFERENCE_PANEL:
            continue
        symbol, bin_index, event_time = classify(row, count=False)
        key = _selection_key(row=row, event_time=event_time)
        target = (
            boundary_targets[symbol]
            if bin_index is None
            else target_counts[(symbol, bin_index)]
        )
        if target <= 0:
            continue
        rank = int(key, 16)
        slot = (symbol, bin_index)
        candidate = (-rank, key, counter, row, event_time)
        counter += 1
        heap = heaps[slot]
        if len(heap) < target:
            heapq.heappush(heap, candidate)
        elif candidate > heap[0]:
            heapq.heapreplace(heap, candidate)

    selected: dict[str, dict[str, Any]] = {}
    for slot, heap in heaps.items():
        for _, key, _, row, event_time in sorted(heap, key=lambda item: (item[1], item[4])):
            selected[key] = {
                "row": row,
                "event_time": event_time,
                "symbol": slot[0],
                "bin_index": slot[1],
                "inclusion_probability": (
                    0.0
                    if slot[1] is None and boundary_counts[slot[0]] == 0
                    else (
                        1.0
                        if slot[1] is None
                        else target_counts[slot] / counts[slot]
                    )
                ),
                "selection_rank": key,
            }
    plan = {
        "schema_version": "dawnstrike.observation.stratified_sampling.v1",
        "method": "fixed_symbol_time_bin_hash_rank",
        "seed": "event-key-sha256-v1",
        "outcome_independent": True,
        "reference_panel": list(_STRATIFIED_REFERENCE_PANEL),
        "bin_count": _STRATIFIED_BIN_COUNT,
        "bin_width_seconds": bin_seconds,
        "max_events": max_events,
        "ordinary_budget": ordinary_budget,
        "per_symbol_budget": per_symbol_budget,
        "counts": {
            symbol: {
                "bins": [counts[(symbol, index)] for index in range(_STRATIFIED_BIN_COUNT)],
                "targets": [
                    target_counts[(symbol, index)] for index in range(_STRATIFIED_BIN_COUNT)
                ],
                "boundary_available": boundary_counts[symbol],
                "boundary_target": boundary_targets[symbol],
                "selected": sum(
                    1
                    for value in selected.values()
                    if value["symbol"] == symbol
                ),
            }
            for symbol in _STRATIFIED_REFERENCE_PANEL
        },
        "expected_selected_count": sum(target_counts.values()) + sum(boundary_targets.values()),
        "present_selected_count": len(selected),
        "source_artifact_count": len(first_artifacts),
    }
    return selected, plan


def build_observation_inputs(
    *,
    capture_receipt_path: str | Path,
    scope_declaration_path: str | Path,
    output_root: str | Path,
    decision_deadline: str,
    repository_root: str | Path | None = None,
    max_events: int = 100_000,
    max_bytes: int = 64 * 1024 * 1024,
    reduction_mode: str = "none",
) -> dict[str, Any]:
    """Create a bound two-scope manifest and bounded derived event streams."""

    if not 1 <= max_events <= 100_000 or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ObservationProducerError("producer bounds are outside the supported range")
    if reduction_mode not in {"none", "bounded_derivative", "stratified_panel"}:
        raise ObservationProducerError("unsupported producer reduction mode")
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
    window_contract = _PROVIDER_WINDOW_CONTRACTS.get((provider.lower(), feed.lower()))
    if window_contract is None:
        raise ObservationProducerError(
            f"no versioned provider window contract for {provider}:{feed}"
        )
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
    if state.get("status") != receipt.get("status"):
        raise ObservationProducerError("capture state status does not match receipt")
    request = state.get("request")
    if not isinstance(request, dict):
        raise ObservationProducerError("capture state request identity is missing")
    for key, expected in (
        ("provider", provider),
        ("feed", feed),
        ("market_date", market_date),
        ("exchange_session_id", session_id),
        ("source_config_hash", source_config_hash),
        ("code_sha", code_sha),
    ):
        if request.get(key) != expected:
            raise ObservationProducerError(f"capture state request identity mismatch: {key}")
    request_start = parse_utc(str(receipt.get("request_start") or ""), label="request_start")
    request_end = parse_utc(str(receipt.get("request_end") or ""), label="request_end")
    completed_at = parse_utc(str(receipt.get("completed_at") or ""), label="completed_at")
    if request_end <= request_start or completed_at < request_end:
        raise ObservationProducerError("capture receipt timing is not chronological")
    page_artifacts: list[dict[str, Any]] = []
    sampling_plan: dict[str, Any] | None = None
    selected_events: dict[str, dict[str, Any]] | None = None
    if reduction_mode == "stratified_panel":
        panel_symbols = tuple(
            str(row.get("symbol") or "").strip().upper()
            for row in entries["liquid_reference_panel"]
        )
        if panel_symbols != _STRATIFIED_REFERENCE_PANEL:
            raise ObservationProducerError(
                "stratified panel requires exactly DIA/IWM/QQQ/SPY/TLT in declaration order"
            )
        selected_events, sampling_plan = _stratified_selection(
            state=state,
            root=state_path.parent,
            provider=provider,
            feed=feed,
            request_start=request_start,
            request_end=request_end,
            max_events=max_events,
        )
    page_items = _iter_page_items(
        state,
        root=state_path.parent,
        provider=provider,
        feed=feed,
        artifacts=page_artifacts,
    )
    manifest_path = root / "universe-manifest.json"
    events_path = root / "raw-events.jsonl"
    boundary_events_path = root / "boundary-events.jsonl"
    tmp_events_path = root / f".raw-events.{os.getpid()}.tmp"
    tmp_boundary_path = root / f".boundary-events.{os.getpid()}.tmp"
    scope_by_symbol = {entry["symbol"]: scope for scope in SCOPES for entry in entries[scope]}
    seen_keys: set[str] = set()
    raw_hasher = hashlib.sha256()
    boundary_hasher = hashlib.sha256()
    raw_event_count = 0
    boundary_event_count = 0
    derived_raw_count = 0
    derived_boundary_count = 0
    source_item_count = 0
    source_timestamp_count = 0
    source_missing_timestamp_count = 0
    source_duplicate_count = 0
    source_out_of_range_count = 0
    source_boundary_count = 0
    truncated = False
    raw_byte_count = 0
    boundary_byte_count = 0
    next_window_identity = f"{session_id}@{request_end.isoformat()}"

    try:
        with (
            tmp_events_path.open("wb") as events_file,
            tmp_boundary_path.open("wb") as boundary_file,
        ):
            for row in page_items:
                source_item_count += 1
                item = row["item"]
                event_raw = item.get("timestamp") or item.get("t") or item.get("time")
                if not event_raw:
                    source_missing_timestamp_count += 1
                    continue
                source_timestamp_count += 1
                event_at = parse_utc(str(event_raw), label="provider event timestamp")
                if event_at < request_start or event_at > request_end:
                    source_out_of_range_count += 1
                    raise ObservationProducerError(
                        "provider event is outside the capture request window"
                    )
                event = {
                    "session_id": session_id,
                    "scope": scope_by_symbol[row["symbol"]],
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
                    "source_page_path": row.get("source_page_path"),
                    "source_item_index": row.get("source_item_index"),
                    "window_contract_id": window_contract["contract_id"],
                }
                event_key = _event_key(
                    symbol=row["symbol"],
                    endpoint=row["endpoint"],
                    event_time=event_at.isoformat(),
                    item=item,
                )
                selection_key = _selection_key(row=row, event_time=event_at.isoformat())
                if (
                    selected_events is not None
                    and row["symbol"] in _STRATIFIED_REFERENCE_PANEL
                    and selection_key not in selected_events
                ):
                    continue
                selected_identity = (
                    selected_events.get(selection_key)
                    if selected_events is not None
                    else None
                )
                if selected_identity is not None:
                    event["selection_rank"] = selected_identity["selection_rank"]
                    event["inclusion_probability"] = selected_identity["inclusion_probability"]
                if event_key in seen_keys:
                    source_duplicate_count += 1
                    continue
                if len(seen_keys) < max_events:
                    seen_keys.add(event_key)
                if event_at == request_end:
                    source_boundary_count += 1
                    boundary_event_count += 1
                    event.update(
                        {
                            "window_classification": (
                                "inclusive_provider_end_excluded_from_current_half_open"
                            ),
                            "derived_window_semantics": DERIVED_WINDOW_SEMANTICS,
                            "next_window_identity": next_window_identity,
                        }
                    )
                    encoded = (
                        json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                    )
                    if boundary_byte_count + raw_byte_count + len(encoded) <= max_bytes:
                        boundary_file.write(encoded)
                        boundary_hasher.update(encoded)
                        boundary_byte_count += len(encoded)
                        derived_boundary_count += 1
                    else:
                        truncated = True
                    continue
                event["window_classification"] = "current_half_open"
                encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                if (
                    raw_event_count >= max_events
                    or raw_byte_count + boundary_byte_count + len(encoded) > max_bytes
                ):
                    if reduction_mode == "none":
                        raise ObservationProducerError(
                            "producer output exceeds bounded event limits"
                        )
                    truncated = True
                    continue
                events_file.write(encoded)
                raw_hasher.update(encoded)
                raw_byte_count += len(encoded)
                raw_event_count += 1
                derived_raw_count += 1
    except Exception:
        for temporary in (tmp_events_path, tmp_boundary_path):
            temporary.unlink(missing_ok=True)
        raise

    artifact_items = sorted(
        [item for item in page_artifacts if item.get("raw_artifact_hash_sha256")],
        key=canonical_json,
    )
    expected_artifact_hash = hashlib.sha256(canonical_json({"items": artifact_items})).hexdigest()
    artifact_identity = receipt.get("artifact_identity")
    if (
        isinstance(artifact_identity, dict)
        and artifact_identity.get("sha256") != expected_artifact_hash
    ):
        for temporary in (tmp_events_path, tmp_boundary_path):
            temporary.unlink(missing_ok=True)
        raise ObservationProducerError("capture artifact identity hash mismatch")
    expected_raw_artifact_hash = hashlib.sha256(
        canonical_json(
            [
                {
                    "endpoint": item["endpoint"],
                    "hash": item["raw_artifact_hash_sha256"],
                    "symbol": item["symbol"],
                }
                for item in artifact_items
                if item.get("raw_artifact_hash_sha256")
            ]
        )
    ).hexdigest()
    if receipt.get("raw_artifact_hash_sha256") not in {None, expected_raw_artifact_hash}:
        for temporary in (tmp_events_path, tmp_boundary_path):
            temporary.unlink(missing_ok=True)
        raise ObservationProducerError("capture raw artifact identity hash mismatch")

    os.replace(tmp_events_path, events_path)
    os.replace(tmp_boundary_path, boundary_events_path)
    raw_events_sha256 = raw_hasher.hexdigest()
    boundary_events_sha256 = boundary_hasher.hexdigest()
    expectation_status = (
        "INCOMPLETE" if truncated or receipt.get("status") != "COMPLETE" else "COMPLETE"
    )
    coverage_class = (
        "BOUNDED_DERIVATIVE"
        if truncated or reduction_mode in {"bounded_derivative", "stratified_panel"}
        else "FULL_DERIVED"
    )
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
            "coverage_class": coverage_class,
            "source_item_count": source_item_count,
            "source_timestamp_count": source_timestamp_count,
            "source_missing_timestamp_count": source_missing_timestamp_count,
            "source_duplicate_count": source_duplicate_count,
            "source_boundary_count": source_boundary_count,
            "derived_event_count": derived_raw_count,
            "derived_boundary_event_count": derived_boundary_count,
            "reduction_mode": reduction_mode,
            "sampling_plan": sampling_plan,
        },
        "source_lineage": {
            "provider": provider,
            "feed": feed,
            "provider_window_contract_schema": WINDOW_CONTRACT_SCHEMA_VERSION,
            "provider_window_contract": window_contract,
            "derived_window_semantics": DERIVED_WINDOW_SEMANTICS,
            "capture_receipt_sha256": receipt_hash,
            "source_config_sha256": source_config_hash,
            "session_id": session_id,
            "code_sha": code_sha,
            "capture_status": receipt.get("status"),
            "request_start": receipt.get("request_start"),
            "request_end": receipt.get("request_end"),
            "completed_at": receipt.get("completed_at"),
            "raw_events_path": str(events_path),
            "raw_events_sha256": raw_events_sha256,
            "boundary_events_path": str(boundary_events_path),
            "boundary_events_sha256": boundary_events_sha256,
            "next_window_identity": next_window_identity,
            "source_item_count": source_item_count,
            "source_out_of_range_count": source_out_of_range_count,
            "coverage_class": coverage_class,
            "sampling_plan": sampling_plan,
        },
        "provider_window_contract": {
            "schema_version": WINDOW_CONTRACT_SCHEMA_VERSION,
            **window_contract,
            "current_derived_window": {
                "start": request_start.isoformat(),
                "end": request_end.isoformat(),
                "semantics": DERIVED_WINDOW_SEMANTICS,
            },
            "next_window_identity": next_window_identity,
        },
        "derivation": {
            "coverage_class": coverage_class,
            "reduction_mode": reduction_mode,
            "source_item_count": source_item_count,
            "source_event_count": source_timestamp_count,
            "derived_event_count": derived_raw_count,
            "boundary_event_count": source_boundary_count,
            "derived_boundary_event_count": derived_boundary_count,
            "source_full_coverage_claim": not truncated and reduction_mode == "none",
        },
        "scopes": scopes,
    }
    _atomic_json(manifest_path, manifest)
    producer_receipt = {
        "schema_version": "dawnstrike.observation.producer_receipt.v1",
        "status": "READY",
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_json(manifest),
        "raw_events_path": str(events_path),
        "raw_events_sha256": raw_events_sha256,
        "capture_receipt_path": str(receipt_path),
        "capture_receipt_sha256": receipt_hash,
        "source_config_sha256": source_config_hash,
        "provider": provider,
        "feed": feed,
        "session_id": session_id,
        "provider_window_contract_schema": WINDOW_CONTRACT_SCHEMA_VERSION,
        "provider_window_contract_id": window_contract["contract_id"],
        "derived_window_semantics": DERIVED_WINDOW_SEMANTICS,
        "boundary_events_path": str(boundary_events_path),
        "boundary_events_sha256": boundary_events_sha256,
        "next_window_identity": next_window_identity,
        "coverage_class": coverage_class,
        "reduction_mode": reduction_mode,
        "source_item_count": source_item_count,
        "source_timestamp_count": source_timestamp_count,
        "source_boundary_count": source_boundary_count,
        "source_duplicate_count": source_duplicate_count,
        "derived_event_count": derived_raw_count,
        "derived_boundary_event_count": derived_boundary_count,
        "source_full_coverage_claim": not truncated and reduction_mode == "none",
        "sampling_plan": sampling_plan,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    _atomic_json(root / "producer-receipt.json", producer_receipt)
    return producer_receipt
