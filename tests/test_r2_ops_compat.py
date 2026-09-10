from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.observation.contracts import canonical_json
from intraday_scanner.observation.producer import (
    ObservationProducerError,
    build_observation_inputs,
)


def _write_capture(tmp_path: Path, items: list[dict]) -> tuple[Path, Path]:
    capture_root = tmp_path / "capture"
    page_path = capture_root / "pages" / "AAA" / "bars" / "page-000000.json"
    page_path.parent.mkdir(parents=True)
    page = {
        "page_number": 0,
        "cursor_in": None,
        "cursor_out": None,
        "provider": "alpaca",
        "feed": "sip",
        "endpoint": "bars",
        "raw_payload_hash_sha256": "d" * 64,
        "previous_page_hash_sha256": None,
        "items": items,
    }
    page_path.write_text(json.dumps(page), encoding="utf-8")
    page_artifact_hash = hashlib.sha256(canonical_json(page)).hexdigest()
    source_config_hash = "a" * 64
    code_sha = "c" * 40
    state = {
        "status": "COMPLETE",
        "request": {
            "provider": "alpaca",
            "feed": "sip",
            "market_date": "2026-01-02",
            "exchange_session_id": "XNYS:2026-01-02:regular",
            "source_config_hash": source_config_hash,
            "code_sha": code_sha,
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
                            "raw_payload_hash_sha256": "d" * 64,
                            "raw_artifact_hash_sha256": page_artifact_hash,
                            "previous_page_hash_sha256": None,
                        }
                    ]
                }
            }
        },
    }
    state_path = capture_root / "capture_run_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    receipt = {
        "schema_version": "dawnstrike.intraday_capture_run.v1",
        "run_id": "capture-r2-boundary",
        "status": "COMPLETE",
        "provider": "alpaca",
        "feed": "sip",
        "symbols": ["AAA"],
        "market_date": "2026-01-02",
        "session_id": "XNYS:2026-01-02:regular",
        "source_config_hash": source_config_hash,
        "code_sha": code_sha,
        "state_path": str(state_path),
        "request_start": "2026-01-02T10:00:00+00:00",
        "request_end": "2026-01-02T11:00:00+00:00",
        "completed_at": "2026-01-02T11:30:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt["receipt_hash_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    receipt_path = capture_root / "capture_run_receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    declaration = {
        "schema_version": "dawnstrike.observation.scope_declaration.v1",
        "universe_generation_id": "generation-r2-boundary",
        "scopes": {
            "original_small_cap_gap": [{"symbol": "AAA", "membership": "selected"}],
            "liquid_reference_panel": [{"symbol": "BBB", "membership": "selected"}],
        },
    }
    declaration_path = tmp_path / "scope.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    return receipt_path, declaration_path


def test_inclusive_provider_end_is_preserved_as_next_window_boundary(tmp_path: Path) -> None:
    receipt, declaration = _write_capture(
        tmp_path,
        [
            {"t": "2026-01-02T10:30:00Z", "c": 10},
            {"t": "2026-01-02T11:00:00Z", "c": 11},
        ],
    )
    result = build_observation_inputs(
        capture_receipt_path=receipt,
        scope_declaration_path=declaration,
        output_root=tmp_path / "producer",
        decision_deadline="2026-01-02T12:00:00Z",
    )
    assert result["status"] == "READY"
    assert result["source_boundary_count"] == 1
    raw = (tmp_path / "producer" / "raw-events.jsonl").read_text(encoding="utf-8")
    boundary = json.loads((tmp_path / "producer" / "boundary-events.jsonl").read_text(encoding="utf-8"))
    assert raw.count("\n") == 1
    assert boundary["window_classification"] == "inclusive_provider_end_excluded_from_current_half_open"
    assert boundary["next_window_identity"] == "XNYS:2026-01-02:regular@2026-01-02T11:00:00+00:00"
    assert result["provider_window_contract_id"] == "alpaca.stock.historical.v1"


def test_provider_future_event_remains_fail_closed(tmp_path: Path) -> None:
    receipt, declaration = _write_capture(
        tmp_path,
        [{"t": "2026-01-02T11:00:01Z", "c": 12}],
    )
    with pytest.raises(ObservationProducerError, match="outside the capture request window"):
        build_observation_inputs(
            capture_receipt_path=receipt,
            scope_declaration_path=declaration,
            output_root=tmp_path / "producer",
            decision_deadline="2026-01-02T12:00:00Z",
        )
