"""Bounded local observer runner for universe-before-decision evidence."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import UniverseManifest, event_id, event_temporal_class, parse_utc, sha256_json
from .store import ObservationLock, ObservationLockError, ObservationStore, ObservationStoreError

_DEFAULT_FORBIDDEN_ROOTS = (Path(r"C:\r\dawnstrike-runtime"), Path(r"C:\r\dawnstrike-state"))
_MAX_EVENTS = 100_000
_MAX_BYTES = 64 * 1024 * 1024
_MAX_PAGES = 10_000
_MAX_RETRIES = 10


class ObservationRunError(ValueError):
    """A run request is unsafe, stale, or malformed."""


@dataclass(frozen=True)
class ObservationRunResult:
    status: str
    receipt_path: Path
    receipt: dict[str, Any]


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def validate_output_root(
    output_root: str | Path,
    *,
    repository_root: str | Path | None = None,
    forbidden_roots: Iterable[str | Path] = _DEFAULT_FORBIDDEN_ROOTS,
) -> Path:
    root = Path(output_root).expanduser().resolve()
    if repository_root is not None and _under(root, Path(repository_root)):
        raise ObservationRunError("observation output must not be under the repository")
    for forbidden in forbidden_roots:
        if _under(root, Path(forbidden)):
            raise ObservationRunError(f"observation output must not be under {forbidden}")
    if root == Path(root.anchor):
        raise ObservationRunError("observation output must be a dedicated directory")
    return root


def _load_json(path: Path, *, label: str, max_bytes: int = 4 * 1024 * 1024) -> dict[str, Any]:
    if not path.is_file():
        raise ObservationRunError(f"{label} is missing: {path}")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ObservationRunError(f"{label} exceeds the bounded read size")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObservationRunError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ObservationRunError(f"{label} must be a JSON object")
    return value


def _write_receipt(store: ObservationStore, receipt: dict[str, Any]) -> Path:
    store.append(store.receipts_path, receipt)
    path = store.root / "run-receipt.json"
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


def _base_receipt(manifest: UniverseManifest, *, status: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "dawnstrike.observation.receipt.v1",
        "status": status,
        "reason": reason,
        "session_id": manifest.session_id,
        "market_date": manifest.market_date,
        "decision_deadline": manifest.decision_deadline.isoformat(),
        "universe_generation_id": manifest.universe_generation_id,
        "universe_manifest_sha256": manifest.manifest_sha256,
        "source_config_sha256": manifest.source_config_sha256,
        "scopes": ["original_small_cap_gap", "liquid_reference_panel"],
        "census_count": len(manifest.entries),
        "captured_event_count": 0,
        "correction_count": 0,
        "delayed_event_count": 0,
        "quarantined_event_count": 0,
        "research_only": True,
        "broker_execution_enabled": False,
        "order_capability": False,
    }


def run_observer(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    source_events_path: str | Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_source_config_sha256: str | None = None,
    repository_root: str | Path | None = None,
    now: datetime | None = None,
    max_pages: int = 1000,
    max_events: int = 10_000,
    max_bytes: int = 4 * 1024 * 1024,
    retries: int = 3,
    stop_path: str | Path | None = None,
) -> ObservationRunResult:
    """Run a bounded source-file capture and append a full census.

    Network/provider acquisition remains owned by the existing read-only
    capture operation.  This sidecar consumes its already-retained event file,
    making the universe/capture boundary independently restartable and testable.
    """

    if not 1 <= max_pages <= _MAX_PAGES:
        raise ObservationRunError(f"max_pages must be between 1 and {_MAX_PAGES}")
    if not 1 <= max_events <= _MAX_EVENTS:
        raise ObservationRunError(f"max_events must be between 1 and {_MAX_EVENTS}")
    if not 1 <= max_bytes <= _MAX_BYTES:
        raise ObservationRunError(f"max_bytes must be between 1 and {_MAX_BYTES}")
    if not 0 <= retries <= _MAX_RETRIES:
        raise ObservationRunError(f"retries must be between 0 and {_MAX_RETRIES}")
    root = validate_output_root(output_root, repository_root=repository_root)
    root.mkdir(parents=True, exist_ok=True)
    store = ObservationStore(root)
    try:
        manifest_value = _load_json(Path(manifest_path), label="universe manifest")
        manifest = UniverseManifest.from_mapping(manifest_value)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ObservationRunError(str(exc)) from exc

    if expected_manifest_sha256 and manifest.manifest_sha256 != expected_manifest_sha256:
        receipt = {
            "schema_version": "dawnstrike.observation.blocked.v1",
            "status": "BLOCKED",
            "reason": "universe_manifest_hash_mismatch",
            "expected_manifest_sha256": expected_manifest_sha256,
            "actual_manifest_sha256": manifest.manifest_sha256,
            "research_only": True,
            "broker_execution_enabled": False,
            "order_capability": False,
        }
        return ObservationRunResult("BLOCKED", _write_receipt(store, receipt), receipt)
    if (
        expected_source_config_sha256
        and manifest.source_config_sha256 != expected_source_config_sha256
    ):
        receipt = _base_receipt(manifest, status="BLOCKED", reason="source_config_hash_mismatch")
        receipt["expected_source_config_sha256"] = expected_source_config_sha256
        return ObservationRunResult("BLOCKED", _write_receipt(store, receipt), receipt)

    receipt = _base_receipt(manifest, status="CAPTURED", reason="bounded_source_processed")
    receipt.update(
        {
            "max_pages": max_pages,
            "max_events": max_events,
            "max_bytes": max_bytes,
            "retries": retries,
            "source_events_path": (
                str(Path(source_events_path).resolve()) if source_events_path else None
            ),
        }
    )
    try:
        with ObservationLock(store.lock_path):
            prior_census = {
                (row.get("scope"), row.get("symbol"))
                for row in store.read_jsonl(store.census_path)
            }
            for row in manifest.census():
                if (row["scope"], row["symbol"]) not in prior_census:
                    store.append(store.census_path, row)

            source_rows: list[dict[str, Any]] = []
            if source_events_path is not None:
                source_path = Path(source_events_path)
                if not source_path.is_file():
                    receipt.update({"status": "FAILED", "reason": "source_unavailable"})
                elif source_path.stat().st_size > max_bytes:
                    receipt.update({"status": "FAILED", "reason": "source_exceeds_byte_bound"})
                else:
                    raw_lines = source_path.read_text(encoding="utf-8").splitlines()
                    if len(raw_lines) > max_pages:
                        receipt.update({"status": "FAILED", "reason": "page_bound_exceeded"})
                    elif len(raw_lines) > max_events:
                        receipt.update({"status": "FAILED", "reason": "event_bound_exceeded"})
                    else:
                        for raw in raw_lines:
                            if raw.strip():
                                value = json.loads(raw)
                                if not isinstance(value, dict):
                                    raise ObservationRunError("source event must be an object")
                                source_rows.append(value)
            if source_events_path is None:
                receipt.update({"status": "MISSED_SESSION", "reason": "source_not_supplied"})
            if receipt["status"] == "CAPTURED":
                entries = {(entry.scope, entry.symbol) for entry in manifest.entries}
                now_utc = (now or datetime.now(UTC)).astimezone(UTC)
                for index, source in enumerate(source_rows):
                    cursor = {
                        "schema_version": "dawnstrike.observation.cursor.v1",
                        "session_id": manifest.session_id,
                        "source_position": index + 1,
                        "last_source_hash": sha256_json(source),
                    }
                    store.write_cursor(cursor)
                    if stop_path is not None and Path(stop_path).exists():
                        receipt.update({"status": "STOPPED", "reason": "stop_requested"})
                        break
                    scope = str(source.get("scope") or "")
                    symbol = str(source.get("symbol") or "").upper()
                    if (scope, symbol) not in entries:
                        store.append(
                            store.root / "quarantine.jsonl",
                            {"reason": "not_in_declared_universe", "source": source},
                        )
                        receipt["quarantined_event_count"] += 1
                        continue
                    event_time = str(source.get("event_time") or "")
                    available_raw = str(source.get("available_at") or "")
                    if not event_time or not available_raw:
                        store.append(
                            store.root / "quarantine.jsonl",
                            {"reason": "missing_source_timing", "source": source},
                        )
                        receipt["quarantined_event_count"] += 1
                        continue
                    available_at = parse_utc(available_raw, label="available_at")
                    temporal = event_temporal_class(
                        available_at=available_at, deadline=manifest.decision_deadline
                    )
                    normalized = {
                        "session_id": manifest.session_id,
                        "scope": scope,
                        "symbol": symbol,
                        "source": str(source.get("source") or "unknown"),
                        "event_time": event_time,
                        "available_at": available_at.isoformat(),
                        "temporal_class": temporal,
                        "kind": str(source.get("kind") or "observation"),
                        "payload": source.get("payload"),
                    }
                    normalized["event_id"] = event_id(
                        session_id=manifest.session_id,
                        scope=scope,
                        symbol=symbol,
                        source=normalized["source"],
                        event_time=event_time,
                        payload=normalized["payload"],
                    )
                    if store.append_event_once(normalized):
                        receipt["captured_event_count"] += 1
                        receipt["correction_count"] = len(
                            store.read_jsonl(store.corrections_path)
                        )
                        if temporal == "delayed":
                            receipt["delayed_event_count"] += 1
                if not source_rows:
                    receipt.update({"status": "EMPTY", "reason": "healthy_empty_source"})
                elif receipt["status"] == "CAPTURED" and now_utc > manifest.decision_deadline:
                    receipt.update({"status": "PARTIAL", "reason": "decision_deadline_elapsed"})
                elif receipt["delayed_event_count"]:
                    receipt.update({"status": "PARTIAL", "reason": "delayed_source_not_timely"})
                elif receipt["captured_event_count"] == 0 and receipt["quarantined_event_count"]:
                    receipt.update({"status": "FAILED", "reason": "no_valid_events"})
    except ObservationLockError:
        receipt.update({"status": "BLOCKED", "reason": "lock_contention"})
    except (
        OSError,
        json.JSONDecodeError,
        ObservationStoreError,
        ObservationRunError,
        ValueError,
    ) as exc:
        receipt.update({"status": "FAILED", "reason": f"collection_error:{exc}"})
    receipt["cursor"] = store.last_cursor()
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    return ObservationRunResult(receipt["status"], _write_receipt(store, receipt), receipt)
