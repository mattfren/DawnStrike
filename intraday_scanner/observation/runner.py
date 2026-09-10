"""Bounded local observer runner for universe-before-decision evidence."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import UniverseManifest, event_id, event_temporal_class, parse_utc
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
    if repository_root is None:
        repository_root = Path(__file__).resolve().parents[2]
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
        "unavailable_event_count": 0,
        "coverage": [],
        "research_only": True,
        "broker_execution_enabled": False,
        "order_capability": False,
    }


def _source_identity(manifest: UniverseManifest, source_path: Path | None) -> dict[str, Any]:
    return {
        "schema_version": "dawnstrike.observation.root_identity.v1",
        "session_id": manifest.session_id,
        "market_date": manifest.market_date,
        "universe_manifest_sha256": manifest.manifest_sha256,
        "source_config_sha256": manifest.source_config_sha256,
        "capture_receipt_sha256": manifest.source_lineage["capture_receipt_sha256"],
        "source_path": str(source_path) if source_path else None,
    }


def _read_source(path: Path, *, max_bytes: int) -> tuple[bytes, list[tuple[int, int, str]]]:
    if path.is_symlink() or not path.is_file():
        raise ObservationRunError("source is not a regular file")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ObservationRunError("source exceeds byte bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ObservationRunError("source is not UTF-8") from exc
    lines: list[tuple[int, int, str]] = []
    position = 0
    for line in text.splitlines(keepends=True):
        end = position + len(line.encode("utf-8"))
        lines.append((position, end, line.rstrip("\r\n")))
        position = end
    return raw, lines


def _validate_cursor(
    cursor: dict[str, Any] | None,
    *,
    manifest: UniverseManifest,
    source_path: Path | None,
    source_bytes: bytes,
) -> None:
    if cursor is None:
        return
    if cursor.get("session_id") != manifest.session_id or cursor.get(
        "manifest_sha256"
    ) != manifest.manifest_sha256:
        raise ObservationRunError("cursor identity does not match this session")
    if cursor.get("source_path") != (str(source_path) if source_path else None):
        raise ObservationRunError("cursor source path changed")
    offset = int(cursor.get("source_offset", 0))
    if offset < 0 or offset > len(source_bytes):
        raise ObservationRunError("source cursor was rewound or truncated")
    prefix_hash = hashlib.sha256(source_bytes[:offset]).hexdigest()
    if prefix_hash != cursor.get("source_prefix_sha256"):
        raise ObservationRunError("source cursor prefix changed")
    if cursor.get("complete") and cursor.get("source_sha256") != hashlib.sha256(
        source_bytes
    ).hexdigest():
        raise ObservationRunError("completed source was replaced")


def _coverage(manifest: UniverseManifest, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in events:
        key = (str(row.get("scope") or ""), str(row.get("symbol") or ""))
        counts[key] = counts.get(key, 0) + 1
    result = []
    for entry in manifest.entries:
        count = counts.get((entry.scope, entry.symbol), 0)
        result.append(
            {
                "scope": entry.scope,
                "symbol": entry.symbol,
                "membership": entry.membership,
                "observation_count": count,
                "status": (
                    "MISSING_INPUT"
                    if entry.membership == "missing_input"
                    else "OBSERVED"
                    if count
                    else "MISSING_OBSERVATION"
                ),
            }
        )
    return result


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

    source_path = Path(source_events_path).resolve() if source_events_path else None
    source_bytes = b""
    source_lines: list[tuple[int, int, str]] = []
    if source_path is not None and source_path.exists():
        source_bytes, source_lines = _read_source(source_path, max_bytes=max_bytes)

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
            "source_events_path": str(source_path) if source_path else None,
            "capture_receipt_sha256": manifest.source_lineage["capture_receipt_sha256"],
        }
    )
    try:
        with ObservationLock(store.lock_path):
            store.ensure_identity(_source_identity(manifest, source_path))
            _validate_cursor(
                store.last_cursor(),
                manifest=manifest,
                source_path=source_path,
                source_bytes=source_bytes,
            )
            prior_census = {
                (row.get("session_id"), row.get("scope"), row.get("symbol"))
                for row in store.read_jsonl(store.census_path)
            }
            for row in manifest.census():
                key = (row["session_id"], row["scope"], row["symbol"])
                if key not in prior_census:
                    store.append(store.census_path, row)

            if source_path is None:
                receipt.update({"status": "MISSED_SESSION", "reason": "source_not_supplied"})
            elif not source_path.is_file():
                receipt.update({"status": "FAILED", "reason": "source_unavailable"})
            elif len(source_lines) > max_pages:
                receipt.update({"status": "FAILED", "reason": "page_bound_exceeded"})
            elif len(source_lines) > max_events:
                receipt.update({"status": "FAILED", "reason": "event_bound_exceeded"})
            elif source_bytes and not any(text.strip() for _, _, text in source_lines):
                receipt.update({"status": "FAILED", "reason": "blank_source"})
            else:
                cursor = store.last_cursor()
                start_offset = int(cursor.get("source_offset", 0)) if cursor else 0
                now_utc = (now or datetime.now(UTC)).astimezone(UTC)
                entries = {(entry.scope, entry.symbol) for entry in manifest.entries}
                for _start, end, raw in source_lines:
                    if end <= start_offset:
                        continue
                    if stop_path is not None and Path(stop_path).exists():
                        receipt.update({"status": "STOPPED", "reason": "stop_requested"})
                        break
                    if not raw.strip():
                        store.write_cursor(
                            {
                                "schema_version": "dawnstrike.observation.cursor.v1",
                                "session_id": manifest.session_id,
                                "manifest_sha256": manifest.manifest_sha256,
                                "source_path": str(source_path),
                                "source_offset": end,
                                "source_prefix_sha256": hashlib.sha256(
                                    source_bytes[:end]
                                ).hexdigest(),
                                "complete": end == len(source_bytes),
                                "source_sha256": hashlib.sha256(source_bytes).hexdigest()
                                if end == len(source_bytes)
                                else None,
                            }
                        )
                        continue
                    source = json.loads(raw)
                    if not isinstance(source, dict):
                        raise ObservationRunError("source event must be an object")
                    scope = str(source.get("scope") or "")
                    symbol = str(source.get("symbol") or "").upper()
                    reason = None
                    if source.get("session_id") != manifest.session_id:
                        reason = "source_session_identity_mismatch"
                    elif source.get("capture_receipt_sha256") != manifest.source_lineage[
                        "capture_receipt_sha256"
                    ]:
                        reason = "source_receipt_identity_mismatch"
                    elif (
                        source.get("provider") != manifest.source_lineage["provider"]
                        or source.get("feed") != manifest.source_lineage["feed"]
                    ):
                        reason = "source_provider_identity_mismatch"
                    elif source.get("source_config_sha256") != manifest.source_config_sha256:
                        reason = "source_config_identity_mismatch"
                    elif (scope, symbol) not in entries:
                        reason = "not_in_declared_universe"
                    event_time = str(source.get("event_time") or "")
                    available_raw = str(source.get("available_at") or "")
                    if reason is None and (not event_time or not available_raw):
                        reason = "missing_source_timing"
                    if reason is None:
                        try:
                            event_at = parse_utc(event_time, label="event_time")
                            available_at = parse_utc(available_raw, label="available_at")
                        except ValueError:
                            reason = "invalid_source_timing"
                    if reason is None and available_at > now_utc:
                        reason = "future_available_at"
                    if reason is not None:
                        store.append(
                            store.root / "quarantine.jsonl",
                            {"reason": reason, "source": source},
                        )
                        receipt["quarantined_event_count"] += 1
                        if reason == "future_available_at":
                            receipt["unavailable_event_count"] += 1
                    else:
                        temporal = event_temporal_class(
                            available_at=available_at, deadline=manifest.decision_deadline
                        )
                        normalized = {
                            "session_id": manifest.session_id,
                            "source_session_id": source["session_id"],
                            "scope": scope,
                            "symbol": symbol,
                            "source": str(source.get("source") or "unknown"),
                            "provider": manifest.source_lineage["provider"],
                            "feed": manifest.source_lineage["feed"],
                            "capture_receipt_sha256": source["capture_receipt_sha256"],
                            "source_config_sha256": source["source_config_sha256"],
                            "code_sha": manifest.source_lineage.get("code_sha"),
                            "event_time": event_at.isoformat(),
                            "available_at": available_at.isoformat(),
                            "ingested_at": now_utc.isoformat(),
                            "temporal_class": temporal,
                            "kind": str(source.get("kind") or "observation"),
                            "payload": source.get("payload"),
                            "source_artifact_hash_sha256": source.get(
                                "source_artifact_hash_sha256"
                            ),
                        }
                        normalized["event_id"] = event_id(
                            session_id=manifest.session_id,
                            scope=scope,
                            symbol=symbol,
                            source=normalized["source"],
                            event_time=normalized["event_time"],
                            payload=normalized["payload"],
                        )
                        store.append_event_once(normalized)
                    store.write_cursor(
                        {
                            "schema_version": "dawnstrike.observation.cursor.v1",
                            "session_id": manifest.session_id,
                            "manifest_sha256": manifest.manifest_sha256,
                            "source_path": str(source_path),
                            "source_offset": end,
                            "source_prefix_sha256": hashlib.sha256(
                                source_bytes[:end]
                            ).hexdigest(),
                            "complete": end == len(source_bytes),
                            "source_sha256": hashlib.sha256(source_bytes).hexdigest()
                            if end == len(source_bytes)
                            else None,
                        }
                    )

                all_events = [
                    row
                    for row in store.read_jsonl(store.events_path)
                    if row.get("session_id") == manifest.session_id
                ]
                receipt["captured_event_count"] = len(all_events)
                receipt["delayed_event_count"] = sum(
                    row.get("temporal_class") == "delayed" for row in all_events
                )
                receipt["correction_count"] = len(store.read_jsonl(store.corrections_path))
                receipt["coverage"] = _coverage(manifest, all_events)
                store.append(store.root / "coverage.jsonl", {
                    "session_id": manifest.session_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "coverage": receipt["coverage"],
                })
                required_missing = [
                    row for row in receipt["coverage"]
                    if row["status"] == "MISSING_OBSERVATION"
                ]
                if not source_bytes:
                    expectation = manifest.collection_expectation
                    if (
                        expectation.get("status") == "COMPLETE"
                        and expectation.get("expected_entries") == len(manifest.entries)
                    ):
                        receipt.update({"status": "EMPTY", "reason": "declared_complete_empty"})
                    else:
                        receipt.update(
                            {"status": "FAILED", "reason": "empty_expectation_incomplete"}
                        )
                elif receipt["status"] == "STOPPED":
                    pass
                elif receipt["unavailable_event_count"] or receipt["delayed_event_count"]:
                    receipt.update({"status": "PARTIAL", "reason": "non_timely_source"})
                elif receipt["captured_event_count"] == 0 and receipt["quarantined_event_count"]:
                    receipt.update({"status": "FAILED", "reason": "no_valid_events"})
                elif required_missing:
                    receipt.update({"status": "PARTIAL", "reason": "declared_coverage_incomplete"})
    except ObservationLockError:
        receipt.update({"status": "BLOCKED", "reason": "lock_contention"})
    except ObservationStoreError as exc:
        reason = str(exc)
        status = "BLOCKED" if any(
            token in reason
            for token in ("identity", "cursor", "source path", "replaced", "truncated")
        ) else "FAILED"
        receipt.update({"status": status, "reason": f"collection_error:{reason}"})
    except ObservationRunError as exc:
        reason = str(exc)
        status = "BLOCKED" if any(
            token in reason
            for token in ("cursor", "source path", "replaced", "truncated")
        ) else "FAILED"
        receipt.update({"status": status, "reason": f"collection_error:{reason}"})
    except (
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        receipt.update({"status": "FAILED", "reason": f"collection_error:{exc}"})
    receipt["cursor"] = store.last_cursor()
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    return ObservationRunResult(receipt["status"], _write_receipt(store, receipt), receipt)
