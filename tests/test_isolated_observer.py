from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.observation.contracts import sha256_json
from intraday_scanner.observation.runner import ObservationRunError, run_observer
from intraday_scanner.observation.store import (
    ObservationLock,
    ObservationStore,
)

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)


def _manifest(tmp_path: Path, *, deadline: str = "2026-01-03T16:00:00Z") -> Path:
    value = {
        "schema_version": "dawnstrike.observation.universe.v1",
        "session_id": "XNYS:2026-01-02:regular",
        "market_date": "2026-01-02",
        "decision_deadline": deadline,
        "universe_generation_id": "universe-2026-01-02-001",
        "source_config_sha256": "a" * 64,
        "scopes": {
            "original_small_cap_gap": [
                {"symbol": "AAA", "membership": "selected", "required_inputs": ["bars"]},
                {"symbol": "BBB", "membership": "rejected", "reason_codes": ["spread"]},
            ],
            "liquid_reference_panel": [
                {"symbol": "CCC", "membership": "unselected"},
                {"symbol": "DDD", "membership": "missing_input", "required_inputs": ["quotes"]},
            ],
        },
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _source(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _event(symbol: str, *, available: str = "2026-01-02T15:00:00Z", value: int = 1) -> dict:
    return {
        "scope": "original_small_cap_gap",
        "symbol": symbol,
        "source": "retained-alpaca-sip",
        "event_time": "2026-01-02T14:59:00Z",
        "available_at": available,
        "kind": "bar",
        "payload": {"close": value},
    }


def test_full_census_raw_events_and_replay_dedupe(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    source = _source(tmp_path, [_event("AAA"), _event("AAA"), _event("BBB")])
    value = json.loads(manifest.read_text(encoding="utf-8"))
    result = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=tmp_path / "evidence",
        expected_manifest_sha256=sha256_json(value),
        now=NOW,
    )
    assert result.status == "CAPTURED"
    assert result.receipt["captured_event_count"] == 2
    assert result.receipt["census_count"] == 4
    census_path = tmp_path / "evidence" / "universe-census.jsonl"
    assert len(ObservationStore(tmp_path / "evidence").read_jsonl(census_path)) == 4

    replay_rows = [_event("BBB"), _event("AAA"), _event("AAA", value=2)]
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in replay_rows), encoding="utf-8"
    )
    replay = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=tmp_path / "evidence",
        now=NOW,
    )
    assert replay.status == "CAPTURED"
    assert replay.receipt["captured_event_count"] == 1
    events_path = tmp_path / "evidence" / "raw-events.jsonl"
    assert len(ObservationStore(tmp_path / "evidence").read_jsonl(events_path)) == 3
    corrections_path = tmp_path / "evidence" / "corrections.jsonl"
    assert len(ObservationStore(tmp_path / "evidence").read_jsonl(corrections_path)) == 1


def test_delayed_source_is_partial_and_never_healthy_empty(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, deadline="2026-01-02T15:00:00Z")
    source = _source(tmp_path, [_event("AAA", available="2026-01-02T15:30:00Z")])
    result = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=tmp_path / "evidence",
        now=NOW,
    )
    assert result.status == "PARTIAL"
    assert result.receipt["delayed_event_count"] == 1


def test_lock_contention_and_unsafe_root_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    output = tmp_path / "evidence"
    output.mkdir()
    lock = ObservationLock(output / ".observer.lock")
    lock.__enter__()
    try:
        result = run_observer(manifest_path=manifest, output_root=output, now=NOW)
        assert result.status == "BLOCKED"
        assert result.receipt["reason"] == "lock_contention"
    finally:
        lock.__exit__(None, None, None)
    with pytest.raises(ObservationRunError):
        run_observer(
            manifest_path=manifest,
            output_root=tmp_path / "repo" / "evidence",
            repository_root=tmp_path / "repo",
        )


def test_interrupted_append_is_recovered_and_stop_is_recorded(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "evidence")
    store.append(store.events_path, {"event_id": "good"})
    store.events_path.write_bytes(store.events_path.read_bytes() + b'{"event_id":"partial"')
    assert store.read_jsonl(store.events_path) == [{"event_id": "good"}]

    manifest = _manifest(tmp_path)
    source = _source(tmp_path, [_event("AAA"), _event("BBB")])
    stop = tmp_path / "stop"
    stop.write_text("operator stop", encoding="utf-8")
    result = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=tmp_path / "stopped",
        stop_path=stop,
        now=NOW,
    )
    assert result.status == "STOPPED"


def test_stale_manifest_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    result = run_observer(
        manifest_path=manifest,
        output_root=tmp_path / "evidence",
        expected_manifest_sha256="f" * 64,
        now=NOW,
    )
    assert result.status == "BLOCKED"
    assert result.receipt["reason"] == "universe_manifest_hash_mismatch"


def test_missing_source_is_missed_and_config_replacement_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    missed = run_observer(
        manifest_path=manifest,
        output_root=tmp_path / "missed",
        now=NOW,
    )
    assert missed.status == "MISSED_SESSION"
    assert missed.receipt["reason"] == "source_not_supplied"

    blocked = run_observer(
        manifest_path=manifest,
        output_root=tmp_path / "blocked",
        expected_source_config_sha256="b" * 64,
        now=NOW,
    )
    assert blocked.status == "BLOCKED"
    assert blocked.receipt["reason"] == "source_config_hash_mismatch"


def test_request_event_bound_is_enforced(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    source = _source(tmp_path, [_event("AAA"), _event("BBB")])
    result = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=tmp_path / "bounded",
        max_events=1,
        now=NOW,
    )
    assert result.status == "FAILED"
    assert result.receipt["reason"] == "event_bound_exceeded"
