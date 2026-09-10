from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.observation.contracts import canonical_json, sha256_json
from intraday_scanner.observation.producer import build_observation_inputs
from intraday_scanner.observation.runner import ObservationRunError, run_observer
from intraday_scanner.observation.store import (
    ObservationLock,
    ObservationStore,
)

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)


def _manifest(tmp_path: Path, *, deadline: str = "2026-01-02T15:00:00Z") -> Path:
    value = {
        "schema_version": "dawnstrike.observation.universe.v1",
        "session_id": "XNYS:2026-01-02:regular",
        "market_date": "2026-01-02",
        "decision_deadline": deadline,
        "universe_generation_id": "universe-2026-01-02-001",
        "source_config_sha256": "a" * 64,
        "collection_expectation": {
            "status": "COMPLETE",
            "expected_entries": 4,
            "receipt_id": "capture-fixture-1",
        },
        "source_lineage": {
            "provider": "alpaca",
            "feed": "sip",
            "capture_receipt_sha256": "b" * 64,
            "source_config_sha256": "a" * 64,
            "session_id": "XNYS:2026-01-02:regular",
            "code_sha": "c" * 40,
            "raw_events_path": str(tmp_path / "events.jsonl"),
            "raw_events_sha256": hashlib.sha256(b"").hexdigest(),
        },
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    manifest_path = next(
        (candidate for candidate in (tmp_path, *tmp_path.parents) if (candidate / "universe.json").is_file()),
        None,
    )
    if manifest_path is not None:
        manifest = json.loads((manifest_path / "universe.json").read_text(encoding="utf-8"))
        manifest["source_lineage"]["raw_events_path"] = str(path.resolve())
        manifest["source_lineage"]["raw_events_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (manifest_path / "universe.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _event(symbol: str, *, available: str = "2026-01-02T11:00:00Z", value: int = 1) -> dict:
    return {
        "session_id": "XNYS:2026-01-02:regular",
        "scope": "original_small_cap_gap",
        "symbol": symbol,
        "source": "retained-alpaca-sip",
        "provider": "alpaca",
        "feed": "sip",
        "event_time": "2026-01-02T14:59:00Z",
        "available_at": available,
        "capture_receipt_sha256": "b" * 64,
        "source_config_sha256": "a" * 64,
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
    assert result.status == "PARTIAL"
    assert result.receipt["captured_event_count"] == 2
    assert result.receipt["census_count"] == 4
    census_path = tmp_path / "evidence" / "universe-census.jsonl"
    assert len(ObservationStore(tmp_path / "evidence").read_jsonl(census_path)) == 4

    replay_rows = [_event("AAA"), _event("AAA"), _event("BBB")]
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in replay_rows), encoding="utf-8"
    )
    replay = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=tmp_path / "evidence",
        now=NOW,
    )
    assert replay.status == "PARTIAL"
    assert replay.receipt["captured_event_count"] == 2
    events_path = tmp_path / "evidence" / "raw-events.jsonl"
    assert len(ObservationStore(tmp_path / "evidence").read_jsonl(events_path)) == 2


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
    assert result.receipt["unavailable_event_count"] == 1


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
    with pytest.raises(ObservationRunError, match="repository"):
        run_observer(
            manifest_path=manifest,
            output_root=Path(__file__).resolve().parents[1] / "evidence",
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


def test_capture_artifact_producer_binds_real_lineage(tmp_path: Path) -> None:
    page_path = tmp_path / "capture" / "pages" / "AAA" / "bars" / "page-000000.json"
    page_path.parent.mkdir(parents=True)
    page_hash = "d" * 64
    page_value = {
        "page_number": 0,
        "cursor_in": None,
        "cursor_out": None,
        "provider": "alpaca",
        "feed": "sip",
        "endpoint": "bars",
        "raw_payload_hash_sha256": page_hash,
        "previous_page_hash_sha256": None,
        "items": [{"t": "2026-01-02T10:00:00Z", "c": 10.5}],
    }
    page_path.write_text(json.dumps(page_value), encoding="utf-8")
    page_artifact_hash = hashlib.sha256(canonical_json(page_value)).hexdigest()
    state_path = tmp_path / "capture" / "capture_run_state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "request": {
                    "provider": "alpaca",
                    "feed": "sip",
                    "market_date": "2026-01-02",
                    "exchange_session_id": "XNYS:2026-01-02:regular",
                    "source_config_hash": "a" * 64,
                    "code_sha": "c" * 40,
                },
                "symbols": {
                    "AAA": {
                        "bars": {
                            "pages": [
                                {
                                    "page_path": str(page_path),
                                    "page_number": 0,
                                    "cursor_in": None,
                                    "cursor_out": None,
                                    "provider": "alpaca",
                                    "feed": "sip",
                                    "endpoint": "bars",
                                    "raw_payload_hash_sha256": page_hash,
                                    "raw_artifact_hash_sha256": page_artifact_hash,
                                    "previous_page_hash_sha256": None,
                                }
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    receipt = {
        "schema_version": "dawnstrike.intraday_capture_run.v1",
        "run_id": "capture-1",
        "status": "COMPLETE",
        "provider": "alpaca",
        "feed": "sip",
        "symbols": ["AAA"],
        "market_date": "2026-01-02",
        "session_id": "XNYS:2026-01-02:regular",
        "source_config_hash": "a" * 64,
        "code_sha": "c" * 40,
        "state_path": str(state_path),
        "request_start": "2026-01-02T10:00:00+00:00",
        "request_end": "2026-01-02T11:00:00+00:00",
        "completed_at": "2026-01-02T11:30:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt["receipt_hash_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    receipt_path = tmp_path / "capture" / "capture_run_receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    declaration = {
        "schema_version": "dawnstrike.observation.scope_declaration.v1",
        "universe_generation_id": "generation-1",
        "scopes": {
            "original_small_cap_gap": [{"symbol": "AAA", "membership": "selected"}],
            "liquid_reference_panel": [{"symbol": "BBB", "membership": "unselected"}],
        },
    }
    declaration_path = tmp_path / "scope.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    output = tmp_path / "producer"
    produced = build_observation_inputs(
        capture_receipt_path=receipt_path,
        scope_declaration_path=declaration_path,
        output_root=output,
        decision_deadline="2026-01-02T12:00:00Z",
    )
    assert produced["status"] == "READY"
    raw_before = Path(produced["raw_events_path"]).read_bytes()
    observer_root = tmp_path / "observer"
    result = run_observer(
        manifest_path=produced["manifest_path"],
        source_events_path=produced["raw_events_path"],
        output_root=observer_root,
        now=NOW,
    )
    assert result.status == "PARTIAL"
    assert Path(produced["raw_events_path"]).read_bytes() == raw_before
    event = ObservationStore(observer_root).read_jsonl(observer_root / "raw-events.jsonl")[0]
    assert event["capture_receipt_sha256"] == produced["capture_receipt_sha256"]
    assert event["session_id"] == "XNYS:2026-01-02:regular"


def test_wrong_source_identity_and_future_availability_are_not_captured(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    wrong = _event("AAA")
    wrong["session_id"] = "XNYS:2099-01-01:regular"
    result = run_observer(
        manifest_path=manifest,
        source_events_path=_source(tmp_path, [wrong]),
        output_root=tmp_path / "wrong",
        now=NOW,
    )
    assert result.status == "FAILED"
    assert result.receipt["captured_event_count"] == 0

    future = run_observer(
        manifest_path=manifest,
        source_events_path=_source(
            tmp_path / "future", [_event("AAA", available="2026-01-02T14:00:00Z")]
        ),
        output_root=tmp_path / "future-output",
        now=NOW,
    )
    assert future.status == "PARTIAL"
    assert future.receipt["unavailable_event_count"] == 1


def test_manifest_calendar_and_output_root_identity_are_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    invalid = json.loads(manifest.read_text(encoding="utf-8"))
    invalid["market_date"] = "2026-01-03"
    invalid["session_id"] = "XNYS:2026-01-03:regular"
    invalid_path = tmp_path / "weekend.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ObservationRunError, match="trading date"):
        run_observer(
            manifest_path=invalid_path,
            output_root=tmp_path / "weekend-output",
            now=NOW,
        )

    first = run_observer(
        manifest_path=manifest,
        source_events_path=_source(tmp_path / "first", [_event("AAA")]),
        output_root=tmp_path / "bound-output",
        now=NOW,
    )
    assert first.status == "PARTIAL"
    changed = json.loads(manifest.read_text(encoding="utf-8"))
    changed["universe_generation_id"] = "replacement-generation"
    changed_path = tmp_path / "replacement.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    blocked = run_observer(
        manifest_path=changed_path,
        source_events_path=_source(tmp_path / "replacement-source", [_event("AAA")]),
        output_root=tmp_path / "bound-output",
        now=NOW,
    )
    assert blocked.status == "BLOCKED"
    assert "identity" in blocked.receipt["reason"]


def test_delayed_replay_whitespace_and_cursor_replacement_stay_truthful(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    delayed_source = _source(
        tmp_path / "delayed", [_event("AAA", available="2026-01-02T15:30:00Z")]
    )
    delayed_output = tmp_path / "delayed-output"
    first = run_observer(
        manifest_path=manifest,
        source_events_path=delayed_source,
        output_root=delayed_output,
        now=datetime(2026, 1, 2, 16, tzinfo=UTC),
    )
    second = run_observer(
        manifest_path=manifest,
        source_events_path=delayed_source,
        output_root=delayed_output,
        now=datetime(2026, 1, 2, 16, tzinfo=UTC),
    )
    assert first.status == second.status == "PARTIAL"
    assert second.receipt["delayed_event_count"] == 1

    whitespace = tmp_path / "whitespace.jsonl"
    whitespace.write_text(" \n\t\n", encoding="utf-8")
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["source_lineage"]["raw_events_path"] = str(whitespace.resolve())
    manifest_value["source_lineage"]["raw_events_sha256"] = hashlib.sha256(
        whitespace.read_bytes()
    ).hexdigest()
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    blank = run_observer(
        manifest_path=manifest,
        source_events_path=whitespace,
        output_root=tmp_path / "whitespace-output",
        now=NOW,
    )
    assert blank.status == "FAILED"
    assert blank.receipt["reason"] == "blank_source"

    source = _source(tmp_path / "cursor", [_event("AAA"), _event("BBB")])
    cursor_output = tmp_path / "cursor-output"
    original = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=cursor_output,
        now=NOW,
    )
    assert original.status == "PARTIAL"
    source.write_text(json.dumps(_event("AAA")) + "\n", encoding="utf-8")
    replaced = run_observer(
        manifest_path=manifest,
        source_events_path=source,
        output_root=cursor_output,
        now=NOW,
    )
    assert replaced.status == "BLOCKED"
    assert replaced.receipt["reason"] == "source_raw_events_hash_mismatch"


def test_cli_process_stop_and_resume_preserve_raw_input(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    source = _source(tmp_path, [_event("AAA")])
    stop = tmp_path / "stop"
    stop.write_text("stop", encoding="utf-8")
    output = tmp_path / "process-output"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts" / "run_isolated_observer.py"),
        "--manifest",
        str(manifest),
        "--source-events",
        str(source),
        "--output-root",
        str(output),
        "--stop-file",
        str(stop),
    ]
    raw_before = source.read_bytes()
    stopped = subprocess.run(command, capture_output=True, text=True, check=False)
    assert stopped.returncode == 0
    assert '"status": "STOPPED"' in stopped.stdout
    stop.unlink()
    resumed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert resumed.returncode == 0
    assert '"status": "PARTIAL"' in resumed.stdout
    assert source.read_bytes() == raw_before
