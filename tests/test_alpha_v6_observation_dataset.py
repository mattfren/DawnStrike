from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.alpha.v6.models import current_training_rows, model_eligibility
from intraday_scanner.alpha.v6.observation_dataset import build_observation_dataset
from intraday_scanner.observation.contracts import UniverseManifest
from intraday_scanner.services.alpha_v6_learning_service import run_alpha_v6_daily_monitor
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _fixture() -> tuple[dict, dict, list[dict], dict]:
    source_hash = "a" * 64
    manifest = {
        "schema_version": "dawnstrike.observation.universe.v1",
        "session_id": "XNYS:2026-01-02:regular",
        "market_date": "2026-01-02",
        "decision_deadline": "2026-01-02T12:00:00+00:00",
        "universe_generation_id": "fixture-generation-1",
        "source_config_sha256": source_hash,
        "collection_expectation": {"status": "COMPLETE", "receipt_id": "receipt-1", "expected_entries": 2},
        "source_lineage": {
            "provider": "fixture-provider",
            "feed": "fixture-feed",
            "capture_receipt_sha256": "b" * 64,
            "session_id": "XNYS:2026-01-02:regular",
            "source_config_sha256": source_hash,
            "raw_events_sha256": "c" * 64,
            "raw_events_path": "fixture-events.jsonl",
        },
        "scopes": {
            "original_small_cap_gap": [{
                "symbol": "AAA", "membership": "selected", "reason_codes": ["ranked"], "required_inputs": ["bars"]
            }],
            "liquid_reference_panel": [{
                "symbol": "BBB", "membership": "missing_input", "reason_codes": ["no_entitlement"], "required_inputs": ["bars"]
            }],
        },
    }
    typed = UniverseManifest.from_mapping(manifest)
    receipt = {
        "schema_version": "dawnstrike.observation.producer_receipt.v1",
        "status": "READY",
        "session_id": typed.session_id,
        "manifest_sha256": typed.manifest_sha256,
        "source_config_sha256": source_hash,
        "capture_receipt_sha256": "b" * 64,
        "raw_events_sha256": "c" * 64,
    }
    decision_at = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    decision = {
        "decision_id": "decision-aaa",
        "market_date": "2026-01-02",
        "decision_at": decision_at.isoformat(),
        "ticker": "AAA",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "model_version": "fixture-model",
        "feature_schema_version": "fixture-feature",
        "feature_hash_sha256": "d" * 64,
        "input_hash_sha256": "e" * 64,
        "source_lineage_hash_sha256": "f" * 64,
        "action": "SHADOW_TRACK",
        "decision_state": "SELECTED",
        "research_only": True,
        "broker_execution_enabled": False,
        "safety_vetoes": [],
        "score_components": {},
        "uncertainty": {},
        "execution_assumptions": {},
        "universe_membership": {"universe_id": "fixture-generation-1", "source_lineage_hash_sha256": "1" * 64},
        "point_in_time": {
            "all_inputs_observed_at_or_before_decision": True,
            "feature_timestamp": "2026-01-02T11:59:00+00:00",
            "feature_available_at": "2026-01-02T11:59:30+00:00",
            "feature_ingested_at": "2026-01-02T11:59:45+00:00",
        },
    }
    events = []
    for index, minutes in enumerate((0, 60, 240, 600, 601)):
        event_at = decision_at + timedelta(minutes=minutes)
        events.append({
            "event_id": f"event-{index}",
            "session_id": typed.session_id,
            "scope": "original_small_cap_gap",
            "symbol": "AAA",
            "source": "fixture:bars",
            "kind": "bars",
            "event_time": event_at.isoformat(),
            "available_at": (event_at + timedelta(seconds=1)).isoformat(),
            "source_artifact_hash_sha256": "c" * 64,
            "payload": {"o": 100.0, "c": 101.0 + index},
        })
    return manifest, receipt, events, decision


def _authenticated_event_path(tmp_path, events: list[dict], receipt: dict):
    raw = b"".join(
        (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        for event in events
    )
    path = tmp_path / "authenticated-events.jsonl"
    path.write_bytes(raw)
    bound_receipt = {**receipt, "raw_events_sha256": hashlib.sha256(raw).hexdigest()}
    return path, bound_receipt


def test_actual_observation_consumer_builds_matured_non_fill_truth_dataset(tmp_path) -> None:
    manifest, receipt, events, decision = _fixture()
    event_path, receipt = _authenticated_event_path(tmp_path, events, receipt)
    packet = build_observation_dataset(
        manifest=manifest,
        producer_receipt=receipt,
        raw_events=event_path,
        decisions=[decision],
        as_of="2026-01-02T22:30:00+00:00",
    )
    assert packet["status"] == "READY"
    assert packet["row_count"] == 1
    label = packet["labels"][0]
    assert label["evidence_class"] == "observational_matured"
    assert label["fill_truth_bound"] is False
    dataset = build_return_dataset(
        decisions=[decision], labels=[], observational_labels=[label]
    )
    assert dataset["row_count"] == 1
    assert dataset["rows"][0]["evidence_class"] == "observational_matured"
    assert dataset["rows"][0]["fill_truth_status"] == "not_applicable_observation"
    assert dataset["research_only"] is True
    assert dataset["broker_execution_enabled"] is False
    training_rows = current_training_rows(dataset["rows"])
    assert len(training_rows) == 1
    assert training_rows[0]["evidence_class"] == "observational_matured"
    assert model_eligibility(dataset["rows"]).eligible_label_count == 1


def test_incomplete_real_r2_receipt_stays_partial_and_noneligible() -> None:
    manifest, receipt, events, decision = _fixture()
    receipt["status"] = "PARTIAL"
    manifest["collection_expectation"]["status"] = "INCOMPLETE"
    receipt["manifest_sha256"] = UniverseManifest.from_mapping(manifest).manifest_sha256
    packet = build_observation_dataset(
        manifest=manifest,
        producer_receipt=receipt,
        raw_events=events,
        decisions=[decision],
        as_of="2026-01-02T22:30:00+00:00",
    )
    assert packet["status"] == "PARTIAL"
    assert packet["row_count"] == 0
    assert packet["coverage"][1]["status"] == "MISSING_INPUT"


def test_forged_receipt_identity_is_rejected() -> None:
    manifest, receipt, events, decision = _fixture()
    receipt["source_config_sha256"] = "f" * 64
    packet = build_observation_dataset(
        manifest=manifest,
        producer_receipt=receipt,
        raw_events=events,
        decisions=[decision],
        as_of="2026-01-02T22:30:00+00:00",
    )
    assert packet["status"] == "INVALID_SCHEMA"
    assert "receipt_source_config_sha256_mismatch" in packet["reason"]


def test_future_or_malformed_event_is_quarantined() -> None:
    manifest, receipt, events, decision = _fixture()
    events[2]["available_at"] = "2026-01-02T11:00:00+00:00"
    packet = build_observation_dataset(
        manifest=manifest,
        producer_receipt=receipt,
        raw_events=events,
        decisions=[decision],
        as_of="2026-01-02T22:30:00+00:00",
    )
    assert packet["status"] == "INVALID_SCHEMA"
    assert packet["row_count"] == 0


def test_daily_monitor_routes_observation_packet_to_dataset_consumer(tmp_path) -> None:
    manifest, receipt, events, decision = _fixture()
    event_path, receipt = _authenticated_event_path(tmp_path, events, receipt)
    store = SQLiteScanStore(tmp_path / "r3.sqlite")
    store.initialize()
    result = run_alpha_v6_daily_monitor(
        store,
        market_date="2026-01-02",
        observation_source={
            "manifest": manifest,
            "producer_receipt": receipt,
            "raw_events": event_path,
            "decisions": [decision],
            "as_of": "2026-01-02T22:30:00+00:00",
        },
    )
    assert result["observation_dataset"]["status"] == "READY"
    assert result["dataset"]["row_count"] == 1
    assert result["dataset"]["observational_row_count"] == 1
