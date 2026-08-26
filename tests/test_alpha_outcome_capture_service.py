from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import intraday_scanner.alpha.canonical_return_truth as return_truth_module
import intraday_scanner.services.alpha_outcome_capture_service as capture_module
from intraday_scanner.alpha.canonical_return_truth import (
    canonical_paper_selection_context,
    canonical_return_truth_valid,
)
from intraday_scanner.alpha.outcome_labeler import label_outcomes
from intraday_scanner.alpha.path_replay import canonical_path_contract_valid
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_COST_MODEL_VERSION,
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
    alphaops_strategy_contract,
)
from intraday_scanner.alpha.v6_shadow import build_v6_shadow_decisions
from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.services.alpha_official_cohort_service import (
    validate_or_recover_official_cohort,
)
from intraday_scanner.services.alpha_outcome_capture_service import (
    capture_sourced_alpha_outcomes,
)
from intraday_scanner.services.learning_service import run_alpha_learning
from intraday_scanner.services.trade_watcher_service import run_trade_watcher
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import canonical_path_result

EASTERN = ZoneInfo("America/New_York")


def _tree(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return directories, files
DAY = "2026-08-03"


def _two_source_config() -> ScannerConfig:
    return ScannerConfig(
        alpaca_api_key_id="test-key-id",
        alpaca_api_secret_key="fixture-value",  # pragma: allowlist secret
    )


def test_sourced_eod_capture_persists_one_canonical_path_receipt(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    rows = _contiguous_bars(overrides={
        "09:30": (9.80, 9.95, 9.75, 9.90),
        "09:31": (9.90, 10.15, 9.88, 10.10),
        "09:32": (10.10, 10.25, 10.05, 10.20),
        "09:36": (10.20, 10.35, 10.15, 10.30),
        "09:46": (10.30, 10.45, 10.25, 10.40),
        "12:00": (10.40, 10.55, 10.35, 10.50),
        "10:01": (12.50, 13.00, 12.40, 12.80),
        "15:59": (10.50, 10.65, 10.45, 10.60),
    })
    payload = _chart_payload(rows)

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: payload,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    assert result["status"] == "complete"
    outcome = result["outcomes"][0]
    assert canonical_path_contract_valid(outcome["path_replay_receipt"])
    assert outcome["source_bar_hash_sha256"]
    assert outcome["no_lookahead"] is True
    assert outcome["broker_execution_enabled"] is False
    assert outcome["source_coverage_complete"] is True
    assert outcome["coverage_expected_minute_count"] == 360
    assert outcome["coverage_observed_minute_count"] == 360

    stored = store.load_signal_outcomes(signal_id="signal-1")
    assert len(stored) == 1
    for key in outcome["replay_input_manifest"]:
        assert stored[0]["replay_input_manifest"][key] == outcome[
            "replay_input_manifest"
        ][key]
    assert stored[0]["path_replay_id"] == outcome["path_replay_id"]
    assert stored[0]["replay_receipt_hash_sha256"] == outcome[
        "replay_receipt_hash_sha256"
    ]
    assert stored[0]["path_replay_receipt"] == outcome["path_replay_receipt"]
    events = store.load_signal_events(signal_id="signal-1")
    assert any(row["event_type"] == "OUTCOME_CAPTURED" for row in events)

    artifact = json.loads(
        (tmp_path / "capture" / "alpha_outcome_capture.json").read_text("utf-8")
    )
    bars = json.loads(
        (tmp_path / "capture" / "alpha_outcome_source_bars.json").read_text("utf-8")
    )
    assert artifact["missing_values_are_zero"] is False
    assert artifact["source_requests"][0]["source_bar_hash_sha256"]
    assert len(bars["NOVA"]) == 390


def test_raw_watcher_intent_adapter_preserves_columns_and_binds_full_trace(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "intent-adapter.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    _run_v5_watcher_entry(tmp_path, db_path)

    raw_records = store.load_trade_intent_records(
        market_date=DAY,
        signal_id=str(signal["signal_id"]),
    )
    assert len(raw_records) == 1
    record = raw_records[0]
    observation_records = store.load_price_observation_records(
        observation_id=str(record["columns"]["source_observation_id"]),
    )
    assert len(observation_records) == 1
    observation_record = observation_records[0]
    assert record["columns"]["action"] == "ENTER_LONG"
    assert record["payload_json"]["action"] == "ENTER_LONG"
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    delivery = store.load_notification_deliveries(
        signal_id=str(signal["signal_id"]),
        limit=10,
    )[0]
    selected = canonical_paper_selection_context(selection, delivery=delivery)

    composite = return_truth_module.canonical_paper_enter_intent_context(
        selected,
        intent_record=record,
        source_observation_record=observation_record,
    )

    receipt = composite["entry_intent_receipt"]
    assert receipt["intent_id"] == record["columns"]["intent_id"]
    assert receipt["decision_trace"] == record["payload_json"]["decision_trace"]
    assert receipt["decision_fingerprint"] == record["payload_json"][
        "decision_fingerprint"
    ]
    assert receipt["source_observation_id"] == record["columns"][
        "source_observation_id"
    ]
    assert receipt["source_bar_hash_sha256"] == record["payload_json"][
        "source_bar_hash_sha256"
    ]
    assert receipt["quantity"] == record["payload_json"]["quantity"]
    assert receipt["notional"] == record["payload_json"]["notional"]
    assert receipt["account_id"] == ALPHAOPS_V5_ACCOUNT_ID
    assert receipt["execution_policy_version"] == (
        ALPHAOPS_V5_POLICY_VERSION
    )
    assert receipt["cost_model_version"] == ALPHAOPS_V5_COST_MODEL_VERSION
    assert receipt["episode_id"] == record["payload_json"]["episode_id"]
    assert receipt["matched_strategy_ids"] == [ALPHAOPS_V5_STRATEGY_ID]
    assert receipt["primary_strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert receipt["episode_dedup_counts"]["status"] == "FROZEN_IDENTITY_ACTIVE"
    binding = return_truth_module.canonical_replay_binding(
        composite,
        kind="alpha_paper_enter_intent",
    )
    assert binding["origin"]["kind"] == "alpha_paper_enter_intent"
    assert binding["origin"]["id"] == record["columns"]["intent_id"]
    assert binding["origin"]["lineage"] == {
        "selection_id": selected["selection_id"],
        "scan_id": selected["scan_id"],
        "signal_id": selected["signal_id"],
        "intent_id": record["columns"]["intent_id"],
    }
    path_entry = return_truth_module.build_canonical_path_entry_receipt(composite)
    assert path_entry["raw_entry_price"] == record["columns"]["decision_price"]
    assert path_entry["effective_at"] == record["columns"]["decision_time"]
    assert path_entry["source_observed_at"] == observation_record["columns"][
        "observed_at"
    ]
    assert path_entry["source_bar_completed_at"] == observation_record[
        "payload_json"
    ]["bar_completed_at"]


@pytest.mark.parametrize("mutation", ("rendered_body", "co_mutated_hashes"))
def test_paper_selection_recomputes_delivery_body_hash(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = SQLiteScanStore(tmp_path / f"delivery-body-{mutation}.sqlite")
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    selection = deepcopy(
        store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    )
    delivery = deepcopy(
        store.load_notification_deliveries(
            signal_id=str(signal["signal_id"]),
            limit=10,
        )[0]
    )
    if mutation == "rendered_body":
        delivery["payload_json"]["body"] = "forged rendered notification"
    else:
        forged_hash = hashlib.sha256(b"forged rendered notification").hexdigest()
        selection["body_sha256"] = forged_hash
        selection["payload_json"]["body_sha256"] = forged_hash
        delivery["body_sha256"] = forged_hash
        delivery["payload_json"]["body_sha256"] = forged_hash

    with pytest.raises(ValueError, match="body hash"):
        canonical_paper_selection_context(selection, delivery=delivery)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("decision", "probability_fallback"),
        ("strategy_id", "alphaops_v4"),
        ("strategy_version", "dawnstrike-alphaops-v4"),
        ("selected_at", "2026-07-01T14:00:00+00:00"),
    ),
)
def test_composite_paper_enter_context_rejects_non_v5_selection(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    db_path = tmp_path / f"intent-selection-policy-{field}.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    _run_v5_watcher_entry(tmp_path, db_path)
    intent = store.load_trade_intent_records(market_date=DAY)[0]
    observation = store.load_price_observation_records(market_date=DAY)[0]
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    delivery = store.load_notification_deliveries(
        signal_id=str(signal["signal_id"]),
        limit=10,
    )[0]
    selected = canonical_paper_selection_context(selection, delivery=delivery)
    selected[field] = value

    with pytest.raises(ValueError, match="clean-edge selection"):
        return_truth_module.canonical_paper_enter_intent_context(
            selected,
            intent_record=intent,
            source_observation_record=observation,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "intent_id",
        "selection_id",
        "scan_id",
        "signal_id",
        "ticker",
        "market_date",
        "decision_at",
        "intent_receipt_hash",
    ),
)
def test_composite_paper_enter_context_rejects_lineage_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    db_path = tmp_path / f"composite-{mutation}.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    _run_v5_watcher_entry(tmp_path, db_path)
    record = store.load_trade_intent_records(market_date=DAY)[0]
    observation_record = store.load_price_observation_records(market_date=DAY)[0]
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    delivery = store.load_notification_deliveries(
        signal_id=str(signal["signal_id"]),
        limit=10,
    )[0]
    selected = canonical_paper_selection_context(selection, delivery=delivery)
    composite = return_truth_module.canonical_paper_enter_intent_context(
        selected,
        intent_record=record,
        source_observation_record=observation_record,
    )
    attacked = deepcopy(composite)
    if mutation == "intent_receipt_hash":
        attacked["entry_intent_receipt"]["receipt_hash_sha256"] = "a" * 64
    elif mutation == "market_date":
        attacked[mutation] = "2026-08-04"
    elif mutation == "decision_at":
        attacked[mutation] = f"{DAY}T14:01:00+00:00"
    elif mutation == "ticker":
        attacked[mutation] = "ATTK"
    else:
        attacked[mutation] = f"forged-{mutation}"

    with pytest.raises(ValueError):
        return_truth_module.canonical_replay_binding(
            attacked,
            kind="alpha_paper_enter_intent",
        )


def test_composite_entry_receipt_keeps_subminute_effective_time(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "subminute-composite.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    _run_v5_watcher_entry(
        tmp_path,
        db_path,
        requested_at="10:00:30",
    )
    intent = store.load_trade_intent_records(market_date=DAY)[0]
    observation = store.load_price_observation_records(market_date=DAY)[0]
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    delivery = store.load_notification_deliveries(
        signal_id=str(signal["signal_id"]),
        limit=10,
    )[0]
    selected = canonical_paper_selection_context(selection, delivery=delivery)
    composite = return_truth_module.canonical_paper_enter_intent_context(
        selected,
        intent_record=intent,
        source_observation_record=observation,
    )

    path_entry = return_truth_module.build_canonical_path_entry_receipt(composite)
    assert path_entry["effective_at"] == f"{DAY}T14:00:30+00:00"
    assert path_entry["source_observed_at"] == f"{DAY}T13:59:00+00:00"
    assert path_entry["source_bar_completed_at"] == f"{DAY}T14:00:00+00:00"


@pytest.mark.parametrize(
    "mutation",
    (
        "column_payload_conflict",
        "stale_fingerprint",
        "source_hash",
        "source_observation",
        "plan",
        "quantity",
        "notional",
        "stand_down",
        "episode_id",
        "matched_strategy_ids",
        "primary_strategy_id",
        "episode_dedup_counts",
    ),
)
def test_raw_watcher_intent_adapter_rejects_one_fact_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    db_path = tmp_path / f"intent-{mutation}.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    _run_v5_watcher_entry(tmp_path, db_path)
    record = deepcopy(
        store.load_trade_intent_records(
            market_date=DAY,
            signal_id=str(signal["signal_id"]),
        )[0]
    )
    observation_record = deepcopy(
        store.load_price_observation_records(
            observation_id=str(record["columns"]["source_observation_id"]),
        )[0]
    )
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    delivery = store.load_notification_deliveries(
        signal_id=str(signal["signal_id"]),
        limit=10,
    )[0]
    selected = canonical_paper_selection_context(selection, delivery=delivery)

    columns = record["columns"]
    payload = record["payload_json"]
    if mutation == "column_payload_conflict":
        payload["action"] = "STAND_DOWN"
    elif mutation == "stale_fingerprint":
        payload["decision_trace"]["computed"]["decision_time"] = (
            f"{DAY}T14:01:00+00:00"
        )
    elif mutation == "source_hash":
        payload["source_bar_hash_sha256"] = "a" * 64
    elif mutation == "source_observation":
        payload["source_observation_id"] = "forged-observation"
    elif mutation == "plan":
        payload["trigger_price"] = float(payload["trigger_price"]) + 1.0
        columns["trigger_price"] = payload["trigger_price"]
    elif mutation == "quantity":
        payload["quantity"] = float(payload["quantity"]) + 1.0
        columns["quantity"] = payload["quantity"]
    elif mutation == "notional":
        payload["notional"] = float(payload["notional"]) + 1.0
        columns["notional"] = payload["notional"]
    elif mutation == "stand_down":
        payload["action"] = "STAND_DOWN"
        payload["lifecycle_state"] = "STAND_DOWN"
        payload["official_paper_eligible"] = False
        columns["action"] = "STAND_DOWN"
        columns["lifecycle_state"] = "STAND_DOWN"
    elif mutation == "episode_id":
        payload["episode_id"] = "episode:" + "f" * 32
    elif mutation == "matched_strategy_ids":
        payload["matched_strategy_ids"] = ["forged_strategy"]
    elif mutation == "primary_strategy_id":
        payload["primary_strategy_id"] = "forged_strategy"
    elif mutation == "episode_dedup_counts":
        payload["episode_dedup_counts"]["raw_pair_count"] += 1
    else:  # pragma: no cover - parameter exhaustiveness
        raise AssertionError(mutation)

    with pytest.raises(ValueError):
        return_truth_module.canonical_paper_enter_intent_context(
            selected,
            intent_record=record,
            source_observation_record=observation_record,
        )


def test_raw_watcher_intent_adapter_rejects_comutated_fake_source_record(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "intent-fake-source.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    _run_v5_watcher_entry(tmp_path, db_path)
    record = deepcopy(store.load_trade_intent_records(market_date=DAY)[0])
    observation = deepcopy(store.load_price_observation_records(market_date=DAY)[0])
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    delivery = store.load_notification_deliveries(
        signal_id=str(signal["signal_id"]),
        limit=10,
    )[0]
    selected = canonical_paper_selection_context(selection, delivery=delivery)

    forged_id = "NOVA:NOVA:csv:forged-observation"
    forged_hash = "a" * 64
    record["columns"]["source_observation_id"] = forged_id
    record["payload_json"]["source_observation_id"] = forged_id
    record["payload_json"]["source_bar_hash_sha256"] = forged_hash
    observation["columns"]["observation_id"] = forged_id
    observation["payload_json"]["source_bar_hash_sha256"] = forged_hash

    with pytest.raises(ValueError):
        return_truth_module.canonical_paper_enter_intent_context(
            selected,
            intent_record=record,
            source_observation_record=observation,
        )


def test_capture_uses_persisted_selection_plan_and_causal_start(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "selection-bound.sqlite"
    store = SQLiteScanStore(db_path)
    selected = {
        **_signal(),
        "_selected_at": f"{DAY}T13:45:00+00:00",
    }
    _persist_selected_signals(store, [selected])
    store.persist_historical_signals(
        [
            {
                **selected,
                "entry_watch_level": 999.0,
                "target_1": 1_000.0,
                "invalidation_level": 998.0,
            }
        ]
    )
    rows = _contiguous_bars(
        default=(9.50, 9.80, 9.40, 9.55),
        overrides={"15:59": (9.65, 9.80, 9.60, 9.75)},
    )

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(rows),
    )

    outcome = result["outcomes"][0]
    manifest = outcome["replay_input_manifest"]
    future_receipt = manifest["future_evidence_receipt"]
    assert manifest["decision_at"] == "2026-08-03T13:45:00+00:00"
    assert manifest["trigger"] == 10.0
    assert manifest["target"] == 10.2
    assert manifest["stop"] == 9.8
    assert manifest["bars"][0]["observed_at"] == "2026-08-03T13:45:00+00:00"
    assert future_receipt["coverage_start"] == "2026-08-03T13:45:00+00:00"
    assert future_receipt["bar_count"] == 375


@pytest.mark.parametrize(
    "case",
    (
        "entry_censored",
        "same_censored",
        "missing_interval",
        "halt",
        "timeout",
        "ordered_target",
        "ordered_stop",
        "source_conflict",
        "corporate_action",
    ),
)
def test_capture_propagates_exact_replay_receipt_and_full_bound_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    calls: list[dict[str, object]] = []
    results = []
    real_selected_evidence = capture_module._selected_source_evidence

    def _selected_evidence(*args: object, **kwargs: object) -> dict[str, object]:
        evidence = dict(real_selected_evidence(*args, **kwargs))
        start = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        evidence.update(
            {
                "source_artifact_identity": f"bars:NOVA:{DAY}",
                "source_conflict": case == "source_conflict",
                "corporate_action_unresolved": case == "corporate_action",
                "halt_intervals": (
                    ((start + timedelta(minutes=1), start + timedelta(minutes=2)),)
                    if case == "halt"
                    else ()
                ),
                "ordered_events": (
                    (
                        {
                            "observed_at": start,
                            "event_type": "TRADE",
                            "price": 10.6,
                        },
                        {
                            "observed_at": start + timedelta(minutes=1, seconds=15),
                            "event_type": "TRADE",
                            "price": 11.0 if case == "ordered_target" else 8.9,
                        },
                    )
                    if case in {"ordered_target", "ordered_stop"}
                    else ()
                ),
                "ordered_evidence_complete": case
                in {"ordered_target", "ordered_stop"},
                "ordered_evidence_identity": (
                    f"trades:NOVA:{DAY}"
                    if case in {"ordered_target", "ordered_stop"}
                    else None
                ),
                "ordered_evidence_hash_sha256": (
                    "f" * 64 if case in {"ordered_target", "ordered_stop"} else None
                ),
                "ordered_evidence_start": (
                    start if case in {"ordered_target", "ordered_stop"} else None
                ),
                "ordered_evidence_end": (
                    start + timedelta(minutes=2)
                    if case in {"ordered_target", "ordered_stop"}
                    else None
                ),
            }
        )
        return evidence

    def _spy_resolve_path(bars: object, **kwargs: object):
        calls.append({"bars": bars, **kwargs})
        result = canonical_path_result(
            market_date=DAY,
            case=case,
            decision_at=kwargs.get("decision_at")
            if isinstance(kwargs.get("decision_at"), datetime)
            else datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
            replay_binding=kwargs.get("replay_binding")
            if isinstance(kwargs.get("replay_binding"), dict)
            else None,
            authenticated_entry=True,
        )
        results.append(result)
        return result

    monkeypatch.setattr(capture_module, "resolve_path", _spy_resolve_path)
    monkeypatch.setattr(
        capture_module,
        "_selected_source_evidence",
        _selected_evidence,
    )
    rows = _contiguous_bars(
        overrides={
            "09:30": (10.0, 10.7, 9.9, 10.6),
            "09:31": (10.6, 11.1, 10.4, 11.0),
            "09:32": (99.0, 100.0, 1.0, 50.0),
            "15:59": (50.0, 51.0, 49.0, 50.0),
        }
    )
    payload = _chart_payload(rows)

    capture = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: payload,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["session_close"] == datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)
    assert call["source_artifact_identity"]
    future_receipt = call["future_evidence_receipt"]
    assert isinstance(future_receipt, dict)
    assert call["source_artifact_identity"] == future_receipt["receipt_id"]
    assert call["source_artifact_hash_sha256"] == future_receipt[
        "receipt_hash_sha256"
    ]
    assert future_receipt["coverage_start"] == "2026-08-03T14:00:00+00:00"
    assert future_receipt["coverage_end"] == "2026-08-03T20:00:00+00:00"
    assert call["source_coverage_complete"] is True
    assert call["source_conflict"] is (case == "source_conflict")
    assert call["corporate_action_unresolved"] is (case == "corporate_action")
    assert bool(call["halt_intervals"]) is (case == "halt")
    assert bool(call["ordered_events"]) is (
        case in {"ordered_target", "ordered_stop"}
    )
    assert call["ordered_evidence_complete"] is (
        case in {"ordered_target", "ordered_stop"}
    )
    assert bool(call["ordered_evidence_identity"]) is (
        case in {"ordered_target", "ordered_stop"}
    )
    assert bool(call["ordered_evidence_hash_sha256"]) is (
        case in {"ordered_target", "ordered_stop"}
    )
    expected = results[0].to_dict()
    outcome = capture["outcomes"][0]
    stored = store.load_signal_outcomes(signal_id="signal-1")[0]
    assert outcome["path_replay_receipt"] == expected
    assert stored["path_replay_receipt"] == expected
    for key, value in expected.items():
        assert outcome[key] == value
        assert stored[key] == value
    if case in {"ordered_target", "ordered_stop", "timeout"}:
        selection = store.load_signal_selections(signal_id="signal-1")[0]
        delivery = store.load_notification_deliveries(signal_id="signal-1")[0]
        selection_context = canonical_paper_selection_context(
            selection,
            delivery=delivery,
        )
        decision = return_truth_module.canonical_paper_enter_intent_context(
            selection_context,
            intent_record=store.load_trade_intent_records(
                market_date=DAY,
                signal_id="signal-1",
            )[0],
            source_observation_record=store.load_price_observation_records(
                market_date=DAY,
            )[0],
        )
        assert canonical_return_truth_valid(outcome, decision=decision)
        for key in (
            "return_truth_schema_version",
            "return_truth_hash_sha256",
            "cost_schema_version",
            "cost_receipt_id",
            "cost_receipt_hash_sha256",
            "cost_receipt",
            "benchmark_symbol",
            "benchmark_return_pct",
            "benchmark_source_bar_hash_sha256",
            "secondary_benchmark_symbol",
            "secondary_benchmark_return_pct",
            "secondary_benchmark_source_bar_hash_sha256",
            "reconciliation_schema_version",
            "reconciliation_receipt_id",
            "reconciliation_receipt_hash_sha256",
            "reconciliation_receipt",
            "causal_decision_identity",
            "eligibility_policy_version",
            "evidence_cohort",
        ):
            assert outcome[key] == stored[key]


def test_authenticated_entry_is_not_reclassified_from_pre_entry_bars(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    rows = _contiguous_bars(
        overrides={
            "09:30": (9.9, 10.3, 9.7, 10.1),
            "09:31": (10.1, 10.4, 10.0, 10.3),
            "10:01": (12.50, 13.00, 12.40, 12.80),
            "15:59": (10.3, 10.5, 10.2, 10.4),
        }
    )
    payload = _chart_payload(rows)

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: payload,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    outcome = result["outcomes"][0]
    assert outcome["path_truth_status"] == "RESOLVED_TARGET_FIRST"
    assert outcome["exit_event"] == "TARGET"
    assert outcome["entry_time"] == "2026-08-03T14:00:00+00:00"
    assert outcome["entry_price"] == 10.05
    assert outcome["activation_status"] == "ACTIVATED"


def test_capture_consumes_replay_exit_without_post_exit_excursion_recompute(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    rows = _contiguous_bars(
        overrides={
            "09:30": (9.9, 10.1, 9.9, 10.05),
            "09:31": (10.05, 10.25, 10.0, 10.2),
            "10:01": (12.50, 13.00, 12.40, 12.80),
            "10:02": (50.0, 60.0, 1.0, 55.0),
            "15:59": (55.0, 56.0, 54.0, 55.0),
        }
    )
    payload = _chart_payload(rows)

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: payload,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    outcome = result["outcomes"][0]
    assert outcome["exit_event"] == "TARGET"
    assert outcome["exit_price"] == 12.75
    assert outcome["exit_time"] == "2026-08-03T14:01:00+00:00"
    assert outcome["mfe_price"] is None
    assert outcome["mae_price"] is None
    assert outcome["high_after_entry"] is None
    assert outcome["low_after_entry"] is None
    assert outcome["bounds"]["mfe_upper"] == 13.0


def test_sourced_capture_refuses_to_infer_an_outcome_before_close(tmp_path: Path) -> None:
    db_path = tmp_path / "early.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    called = False

    def unexpected_fetch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T15:59:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=unexpected_fetch,
    )

    assert result["status"] == "session_incomplete"
    assert called is False
    assert store.load_signal_outcomes() == []


def test_sourced_capture_fails_closed_without_exact_session_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-selection.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_historical_signals([_signal()])

    with pytest.raises(SnapshotValidationError, match="official cohort"):
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            config=ScannerConfig(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
        )


def test_sourced_capture_fails_closed_on_partially_persisted_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "partial-selection.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_signal_selections(
        [
            {
                "selection_id": "selection-missing-signal",
                "scan_id": "scan-partial",
                "signal_id": "missing-historical-signal",
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "clean_edge",
                "selected_at": f"{DAY}T13:00:00Z",
                "event_key": "alphaops:partial:alpha_morning_watch",
                "body_sha256": "partial-body-hash",
            }
        ]
    )

    with pytest.raises(SnapshotValidationError, match="official cohort"):
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            config=ScannerConfig(),
        )


@pytest.mark.parametrize("mutation", ("deleted_member", "changed_member"))
def test_sourced_capture_requires_exact_frozen_official_cohort_membership(
    tmp_path: Path,
    mutation: str,
) -> None:
    db_path = tmp_path / "cohort.sqlite"
    store = SQLiteScanStore(db_path)
    signal = _v5_signal()
    _persist_selected_signals(store, [signal])
    selection = store.load_signal_selections(signal_id=str(signal["signal_id"]))[0]
    frozen = validate_or_recover_official_cohort(
        store,
        market_date=DAY,
        strategy_id=str(selection["strategy_id"]),
        strategy_version=str(selection["strategy_version"]),
        persist_recovery=True,
    )
    assert frozen.errors == ()
    with sqlite3.connect(db_path) as connection:
        if mutation == "deleted_member":
            connection.execute(
                "DELETE FROM signal_selections WHERE selection_id = ?",
                (selection["selection_id"],),
            )
        else:
            connection.execute(
                "UPDATE signal_selections SET body_sha256 = ? WHERE selection_id = ?",
                ("f" * 64, selection["selection_id"]),
            )
    before = _tree(tmp_path)

    with pytest.raises(SnapshotValidationError, match="official cohort"):
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            persist=True,
            config=ScannerConfig(),
        )

    assert _tree(tmp_path) == before


def test_sourced_capture_does_not_freeze_noncanonical_recovery_candidate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candidate.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    selection = store.load_signal_selections(signal_id="signal-1")[0]
    payload = dict(selection["payload_json"])
    payload["decision_payload"] = {"decision": "attacker"}
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE signal_selections SET payload_json = ? WHERE selection_id = ?",
            (
                json.dumps(payload, sort_keys=True),
                selection["selection_id"],
            ),
        )
    before = _tree(tmp_path)

    with pytest.raises(SnapshotValidationError, match="not canonical"):
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            persist=True,
            config=ScannerConfig(),
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM official_strategy_cohorts"
        ).fetchone() == (0,)
    assert _tree(tmp_path) == before


def test_current_v6_builder_decision_is_quarantined_without_current_context(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v6-quarantine.sqlite"
    store = SQLiteScanStore(db_path)
    selected = _signal()
    _persist_selected_signals(store, [selected])
    decision = build_v6_shadow_decisions(
        signals=[
            {
                **selected,
                "timestamp": selected["generated_at"],
                "can_alert": True,
                "alert_gate_status": "PASS",
                "alpha_score": 80.0,
            }
        ],
        feature_vectors=[
            {
                "ticker": "NOVA",
                "timestamp": f"{DAY}T12:59:00+00:00",
                "config_hash": "f" * 64,
                "feature_json": {
                    "playbook_setup": {"setup_key": "gap-breakout"},
                    "liquidity_execution": {"spread_pct": 0.1},
                },
            }
        ],
        source_summary={
            "status": "success",
            "primary_source": "licensed-primary",
            "source_artifact_identity": f"v6-source:{DAY}",
            "source_artifact_hash_sha256": "a" * 64,
        },
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker={
            "NOVA": {
                "universe_id": "v6u-fixture",
                "status": "ACTIVE",
                "source_lineage_hash_sha256": "b" * 64,
            }
        },
    )[0]
    assert decision["action"] == "SHADOW_TRACK"
    decision.pop("evidence_cohort")
    assert "evidence_cohort" not in decision
    store.persist_alpha_v6_decisions([decision])
    rows = _contiguous_bars(
        overrides={
            "09:30": (9.8, 9.95, 9.75, 9.9),
            "09:31": (9.9, 10.15, 9.88, 10.1),
            "09:32": (10.1, 10.25, 10.05, 10.2),
        }
    )

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(rows),
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    shadow_signal_id = str(decision["shadow_signal_id"])
    diagnostic = next(
        row for row in result["diagnostics"] if row["signal_id"] == shadow_signal_id
    )
    assert diagnostic["status"] == "ineligible_canonical_path_context"
    assert "evidence_cohort" in diagnostic["detail"]
    assert all(row["signal_id"] != shadow_signal_id for row in result["outcomes"])


def test_non_clean_edge_paper_selection_is_quarantined_from_current_return(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fallback-tier.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    selection = store.load_signal_selections(signal_id="signal-1")[0]
    payload = dict(selection["payload_json"])
    payload["decision"] = "probability_fallback"
    payload["decision_payload"] = {
        "decision": "probability_fallback",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE signal_selections SET decision = ?, payload_json = ? "
            "WHERE selection_id = ?",
            (
                "probability_fallback",
                json.dumps(payload, sort_keys=True),
                selection["selection_id"],
            ),
        )
        connection.execute(
            "UPDATE notification_delivery_memberships "
            "SET decision = ?, payload_json = ? "
            "WHERE selection_id = ?",
            (
                "probability_fallback",
                json.dumps({**payload, "delivery_status": "delivered"}, sort_keys=True),
                selection["selection_id"],
            ),
        )
    rows = _contiguous_bars(
        overrides={
            "09:30": (9.8, 9.95, 9.75, 9.9),
            "09:31": (9.9, 10.15, 9.88, 10.1),
            "09:32": (10.1, 10.25, 10.05, 10.2),
        }
    )

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(rows),
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    assert result["outcomes"] == []
    assert result["diagnostics"][0]["status"] == (
        "ineligible_incomplete_canonical_return_truth"
    )
    assert store.load_signal_outcomes(signal_id="signal-1") == []


def test_sourced_capture_allows_explicit_recorded_no_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "no-trade.sqlite"
    store = SQLiteScanStore(db_path)
    signal_id = f"no_trade:{DAY}"
    selected_at = f"{DAY}T13:00:00+00:00"
    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    body_sha256 = hashlib.sha256(b"canonical-no-trade").hexdigest()
    selection = {
        "selection_id": "selection-no-trade",
        "scan_id": "scan-no-trade",
        "signal_id": signal_id,
        "ticker": "NO_TRADE",
        "rank": 0,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": "official_telegram",
        "decision": "no_trade",
        "selected_at": selected_at,
        "event_key": "alphaops:no-trade:alpha_no_trade",
        "body_sha256": body_sha256,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    selection["payload_json"] = {
        **selection,
        "signal": {
            "signal_id": signal_id,
            "scan_id": "scan-no-trade",
            "ticker": "NO_TRADE",
            "market_date": DAY,
        },
        "decision_payload": {
            "decision": "no_trade",
            "no_trade": True,
            "research_only": True,
            "broker_execution_enabled": False,
        },
    }
    store.persist_signal_selections([selection])
    delivery = {
        **selection,
        "membership_id": "delivery-no-trade",
        "channel": "telegram",
        "delivery_status": "delivered",
        "attempted_at": selected_at,
        "delivered_at": selected_at,
    }
    delivery["payload_json"] = {
        **delivery,
        "body": "canonical-no-trade",
        "research_only": True,
    }
    store.persist_notification_deliveries([delivery])

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
    )

    assert result["status"] == "no_targets"
    assert result["signal_count"] == 0


def test_verified_not_triggered_is_persisted_but_never_learned(tmp_path: Path) -> None:
    db_path = tmp_path / "not-triggered.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    payload = _chart_payload(_contiguous_bars(
        default=(9.50, 9.80, 9.40, 9.55),
        overrides={"15:59": (9.65, 9.80, 9.60, 9.75)},
    ))

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: payload,
    )

    outcome = store.load_signal_outcomes()[0]
    assert result["not_triggered_count"] == 1
    assert outcome["outcome_status"] == "not_triggered"
    assert outcome["learning_eligible"] is True
    assert outcome["activation_label_eligible"] is True
    assert outcome["retrospective_research_eligible"] is False
    assert outcome["prospective_promotion_eligible"] is False
    assert outcome["entry_price"] is None
    assert outcome["close_price"] is None
    learning = run_alpha_learning(store)
    assert learning["status"] == "complete"
    assert learning["sourced_outcomes_considered"] == 0
    assert learning["return_learning_eligible"] is False


def test_missing_source_bar_truth_remains_unresolved_and_null(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    payload = _chart_payload(_contiguous_bars(overrides={
        "09:30": (9.80, None, 9.75, 9.90),
        "15:59": (10.10, 10.20, 10.00, 10.15),
    }))

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: payload,
    )

    assert result["status"] == "partial"
    assert result["ineligible_count"] == 1
    assert result["diagnostics"][0]["status"] == "ineligible_incomplete_source_bars"
    assert store.load_signal_outcomes() == []
    assert result["required_stage_failed"] is True
    assert result["capture_attempts"]["terminal_missing_count"] == 1
    attempts = store.load_outcome_capture_attempts(market_date=DAY)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "terminal_missing"
    assert attempts[0]["learning_eligible"] is False
    assert attempts[0]["error_code"] == "ineligible_incomplete_source_bars"


def test_bounded_secondary_provider_fallback_captures_full_attribution(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fallback.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    rows = _contiguous_bars(overrides={
        "09:30": (9.80, 9.95, 9.75, 9.90),
        "09:31": (9.90, 10.15, 9.88, 10.10),
        "09:32": (10.10, 10.25, 10.05, 10.20),
        "10:01": (12.50, 13.00, 12.40, 12.80),
        "15:59": (10.20, 10.40, 10.15, 10.30),
    })
    primary_calls = 0

    def unavailable_primary(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal primary_calls
        primary_calls += 1
        raise DataProviderError("primary unavailable")

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=unavailable_primary,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
        provider_attempt_limit=2,
    )

    assert result["status"] == "partial"
    assert primary_calls == 6  # two bounded attempts for NOVA, SPY, and IWM
    assert result["outcomes"] == []
    assert result["diagnostics"][0]["status"] == (
        "ineligible_incomplete_canonical_return_truth"
    )
    assert result["required_stage_failed"] is True


def test_alpaca_first_mode_preserves_yahoo_as_secondary_reconciliation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpaca-first.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    rows = _contiguous_bars(overrides={
        "09:30": (9.80, 9.95, 9.75, 9.90),
        "09:31": (9.90, 10.15, 9.88, 10.10),
        "10:01": (12.50, 13.00, 12.40, 12.80),
        "15:59": (10.20, 10.40, 10.15, 10.30),
    })
    yahoo_calls = 0

    def unavailable_yahoo(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal yahoo_calls
        yahoo_calls += 1
        raise DataProviderError("secondary unavailable")

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(
            alpaca_api_key_id="test-key-id",
            alpaca_api_secret_key="fixture-value",  # pragma: allowlist secret
            outcome_capture_provider_order="alpaca,yahoo",
        ),
        fetcher=unavailable_yahoo,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
        provider_attempt_limit=2,
    )

    assert result["status"] == "partial"
    assert yahoo_calls == 6  # Two bounded reconciliation attempts for NOVA, SPY, and IWM.
    assert result["outcomes"] == []
    assert result["diagnostics"][0]["status"] == (
        "ineligible_incomplete_canonical_return_truth"
    )


def test_v5_complete_candidate_without_authenticated_entry_is_not_labeled(
    tmp_path: Path,
) -> None:
    day = "2026-07-31"
    db_path = tmp_path / "v5-non-fill.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_v5_signal(day)])

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=day,
        requested_at=f"{day}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(
            _contiguous_bars(
                day=day,
                overrides={
                    "09:31": (9.90, 10.15, 9.88, 10.10),
                    "09:32": (10.10, 10.25, 10.05, 10.20),
                },
            )
        ),
    )

    assert result["outcomes"] == []
    assert result["diagnostics"][0]["status"] == (
        "ineligible_incomplete_canonical_return_truth"
    )
    assert result["required_stage_failed"] is True


def test_outcome_matching_does_not_reuse_a_ticker_outcome_across_scans() -> None:
    signals = [
        {"signal_id": "a", "scan_id": "scan-a", "ticker": "NOVA"},
        {"signal_id": "b", "scan_id": "scan-b", "ticker": "NOVA"},
    ]
    outcomes = [{
        "signal_id": "b",
        "scan_id": "scan-b",
        "ticker": "NOVA",
        "entry_price": 10.0,
        "high_after_entry": 11.0,
        "low_after_entry": 9.5,
        "close_price": 10.5,
        "source": "test_source",
    }]

    labels = label_outcomes(signals, outcomes)

    assert [row["signal_id"] for row in labels] == ["b"]


def test_sourced_capture_uses_published_early_close(tmp_path: Path) -> None:
    early_day = "2026-11-27"
    db_path = tmp_path / "early-close.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(
        store,
        [_canonical_signal(early_day)],
        authenticated_entry=True,
    )
    rows = _contiguous_bars(
        day=early_day,
        close_clock="12:59",
        overrides={
            "09:30": (9.95, 10.10, 9.90, 10.05),
            "10:01": (12.50, 13.00, 12.40, 12.80),
            "12:59": (10.05, 10.30, 10.00, 10.25),
        },
    )
    payload = _chart_payload(rows)

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=early_day,
        requested_at=f"{early_day}T13:05:00-05:00",
        out_dir=tmp_path / "capture",
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: payload,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    assert result["status"] == "complete"
    assert result["market_session"]["status"] == "early_close"
    assert result["market_session"]["close_time_et"] == "13:00"
    assert result["outcomes"][0]["exit_time"] == "2026-11-27T15:01:00+00:00"


def test_sparse_or_gapped_bars_never_become_conclusive(tmp_path: Path) -> None:
    complete = _contiguous_bars()
    cases = {
        "sparse": [complete[0], complete[150], complete[-1]],
        "missing_start": complete[1:],
        "missing_middle": complete[:100] + complete[101:],
        "missing_final": complete[:-1],
    }
    expected = {
        "sparse": "ineligible_bar_gap",
        "missing_start": "ineligible_missing_start_bar",
        "missing_middle": "ineligible_bar_gap",
        "missing_final": "ineligible_missing_final_bar",
    }
    for name, bars in cases.items():
        db_path = tmp_path / f"{name}.sqlite"
        store = SQLiteScanStore(db_path)
        _persist_selected_signals(store, [_signal()])
        result = capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / name,
            config=ScannerConfig(),
            fetcher=lambda *_args, _bars=bars, **_kwargs: _chart_payload(_bars),
        )

        assert result["status"] == "partial"
        assert result["diagnostics"][0]["status"] == expected[name]
        assert result["not_triggered_count"] == 0
        assert store.load_signal_outcomes() == []


def test_selection_timestamp_supersedes_untrusted_signal_generated_at(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "malformed.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(
        store,
        [{**_v5_signal(), "generated_at": "not-a-timestamp"}],
    )

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "malformed",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    )

    assert result["status"] == "partial"
    assert result["diagnostics"][0]["status"] == "not_triggered"
    stored = store.load_signal_outcomes()
    assert len(stored) == 1
    assert stored[0]["outcome_status"] == "not_triggered"
    assert stored[0]["learning_eligible"] is True
    assert stored[0]["activation_label_eligible"] is True

    naive_db = tmp_path / "timezone-naive.sqlite"
    naive_store = SQLiteScanStore(naive_db)
    _persist_selected_signals(
        naive_store,
        [{**_v5_signal(), "generated_at": f"{DAY}T10:00:00"}],
    )
    with pytest.raises(SnapshotValidationError, match="selected_at is not canonical UTC"):
        capture_sourced_alpha_outcomes(
            db_path=naive_db,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "timezone-naive",
            config=ScannerConfig(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
        )


def test_malformed_ohlc_never_becomes_conclusive(tmp_path: Path) -> None:
    db_path = tmp_path / "malformed-ohlc.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    bars = _contiguous_bars(overrides={
        "10:15": (9.90, 9.80, 9.85, 9.88),
    })

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(bars),
    )

    assert result["status"] == "partial"
    assert result["diagnostics"][0]["status"] == "ineligible_malformed_ohlc"
    assert store.load_signal_outcomes() == []


def test_all_eligible_same_ticker_signals_are_captured(tmp_path: Path) -> None:
    db_path = tmp_path / "multiple-signals.sqlite"
    store = SQLiteScanStore(db_path)
    second = {
        **_signal(),
        "signal_id": "signal-2",
        "scan_id": "scan-2",
        "alpha_signal_id": "alpha-2",
        "generated_at": f"{DAY}T13:45:00Z",
    }
    _persist_selected_signals(store, [_signal(), second])

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    )

    assert result["signal_count"] == 2
    assert {row["signal_id"] for row in result["outcomes"]} == {
        "signal-1",
        "signal-2",
    }


def test_replaced_outcome_event_identity_tracks_source_evidence_revision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "event-revision.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    first_bars = _contiguous_bars()

    def capture(bars: list[dict[str, Any]]) -> None:
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            persist=True,
            replace=True,
            config=ScannerConfig(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(bars),
        )

    capture(first_bars)
    capture(first_bars)
    assert len(store.load_signal_events(signal_id="signal-1")) == 1

    revised_bars = list(first_bars)
    revised_bars[-1] = {**revised_bars[-1], "close": 9.93}
    capture(revised_bars)

    events = [
        row
        for row in store.load_signal_events(signal_id="signal-1")
        if row["event_type"] == "OUTCOME_RESOLVED"
    ]
    assert len(events) == 2
    assert len({row["event_id"] for row in events}) == 2


def test_outcome_and_event_persistence_is_atomic(tmp_path: Path) -> None:
    db_path = tmp_path / "atomic.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    outcome = {
        "signal_id": "signal-1",
        "market_date": DAY,
        "ticker": "NOVA",
        "outcome_source": "test",
        "imported_at": f"{DAY}T20:05:00Z",
        "outcome_status": "complete_sourced",
    }
    invalid_event = {
        "event_id": "event-1",
        "signal_id": "signal-1",
        "event_type": "OUTCOME_CAPTURED",
        "event_timestamp": f"{DAY}T20:05:00Z",
        "source": "test",
        "payload_json": {"not_json_serializable": object()},
    }

    with pytest.raises(TypeError):
        store.persist_signal_outcomes_with_events([outcome], [invalid_event])

    assert store.load_signal_outcomes() == []
    assert store.load_signal_events(signal_id="signal-1") == []


def test_existing_sourced_outcome_repairs_a_missing_audit_event(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    common = {
        "db_path": db_path,
        "market_date": DAY,
        "requested_at": f"{DAY}T16:05:00-04:00",
        "config": ScannerConfig(),
        "fetcher": lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    }
    preview = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "preview",
        persist=False,
    )
    store.persist_signal_outcomes(preview["outcomes"])
    assert store.load_signal_events(signal_id="signal-1") == []

    repaired = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "repair",
        persist=True,
    )

    assert repaired["status"] == "already_captured"
    assert repaired["audit_events"]["inserted"] == 1
    assert len(store.load_signal_events(signal_id="signal-1")) == 1


def test_legacy_complete_outcome_is_quarantined_and_revision_is_deferred(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    legacy = {
        "signal_id": "signal-1",
        "market_date": DAY,
        "ticker": "NOVA",
        "outcome_source": "legacy-capture",
        "imported_at": f"{DAY}T20:05:00+00:00",
        "outcome_status": "complete_sourced",
        "automatic_sourced_data": True,
        "learning_eligible": True,
    }
    store.persist_signal_outcomes([legacy])
    before = store.load_signal_outcomes(signal_id="signal-1")
    events_before = store.load_signal_events(signal_id="signal-1")
    rows = _contiguous_bars(
        overrides={
            "09:30": (9.8, 9.95, 9.75, 9.9),
            "09:31": (9.9, 10.15, 9.88, 10.1),
            "09:32": (10.1, 10.25, 10.05, 10.2),
            "10:01": (12.50, 13.00, 12.40, 12.80),
        }
    )

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(rows),
        fallback_fetcher=lambda *_args, **_kwargs: rows,
    )

    assert result["legacy_outcome_quarantined_count"] == 1
    assert result["outcome_revision_required"] is True
    assert result["canonical_source_available_revision_deferred_count"] == 1
    assert any(
        row["status"] == "canonical_source_available_revision_deferred"
        for row in result["diagnostics"]
    )
    assert result["outcomes"] == []
    assert store.load_signal_outcomes(signal_id="signal-1") == before
    assert store.load_signal_events(signal_id="signal-1") == events_before


def test_sourced_capture_preview_preserves_existing_database_bytes(tmp_path: Path) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "preview.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_canonical_signal()], authenticated_entry=True)
    before = _tree(db_root)

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "preview",
        persist=False,
        config=_two_source_config(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(
            _contiguous_bars(
                overrides={"10:01": (12.50, 13.00, 12.40, 12.80)}
            )
        ),
        fallback_fetcher=lambda *_args, **_kwargs: _contiguous_bars(
            overrides={"10:01": (12.50, 13.00, 12.40, 12.80)}
        ),
    )

    assert result["status"] == "complete"
    assert (tmp_path / "preview" / "alpha_outcome_capture.json").is_file()
    assert _tree(db_root) == before


def test_mixed_repair_and_new_event_accounting_is_aggregated(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed-repair.sqlite"
    store = SQLiteScanStore(db_path)
    second = {
        **_signal(),
        "signal_id": "signal-2",
        "scan_id": "scan-2",
        "alpha_signal_id": "alpha-2",
        "generated_at": f"{DAY}T13:45:00Z",
    }
    _persist_selected_signals(store, [_signal(), second])
    common = {
        "db_path": db_path,
        "market_date": DAY,
        "requested_at": f"{DAY}T16:05:00-04:00",
        "config": ScannerConfig(),
        "fetcher": lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    }
    preview = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "preview",
        persist=False,
    )
    first = next(row for row in preview["outcomes"] if row["signal_id"] == "signal-1")
    store.persist_signal_outcomes([first])

    result = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "capture",
        persist=True,
    )

    assert result["audit_events"] == {
        "inserted": 2,
        "skipped": 0,
        "repaired_inserted": 1,
        "repaired_skipped": 0,
        "new_inserted": 1,
        "new_skipped": 0,
    }
    assert len(store.load_signal_outcomes()) == 2


def _signal(day: str = DAY) -> dict[str, Any]:
    return {
        "signal_id": "signal-1",
        "scan_id": "scan-1",
        "alpha_signal_id": "alpha-1",
        "generated_at": f"{day}T13:00:00Z",
        "market_date": day,
        "ticker": "NOVA",
        "company": "Nova Research",
        "rank": 1,
        "source": "public_web",
        "source_url": "https://example.test/nova",
        "source_confidence": 90.0,
        "data_source_kind": "public_free_shadow",
        "model_version": "dawnstrike-alphaops-v4",
        "config_hash": "config-hash",
        "primary_setup": "gap-breakout",
        "setup_grade": "A",
        "signal_label": "WATCH",
        "entry_watch_level": 10.0,
        "entry_trigger_type": "breakout_confirmation",
        "entry_condition": "Watch above 10.0",
        "confirmation_condition": "Sustained volume",
        "exit_line": 9.8,
        "invalidation_level": 9.8,
        "target_1": 10.2,
        "target_2": 10.5,
        "risk_flags_json": [],
        "avoid_reasons_json": [],
        "catalyst_summary": "Sourced catalyst",
        "telegram_event_key": "",
        "was_alerted": True,
        "no_trade_reason": "",
        "raw_payload_json": {
            "can_alert": True,
            "trade_plan_blocks_alert": False,
            "setup_key": "gap-breakout",
            "alpha_score": 80.0,
        },
    }


def _v5_signal(day: str = DAY) -> dict[str, Any]:
    signal = {
        **_signal(day),
        "signal_id": "signal-v5-entry",
        "scan_id": "scan-v5-entry",
        "alpha_signal_id": "alpha-v5-entry",
        "model_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "source": "verified_snapshot",
        "source_confidence": 92.0,
        "source_count": 3,
        "source_quality_status": "verified",
        "stale_data_flag": False,
        "primary_setup": "Momentum",
        "entry_watch_level": 10.0,
        "invalidation_level": 9.0,
        "exit_line": 9.0,
        "target_1": 12.75,
        "target_2": 13.5,
        "target_basis_kind": "sourced_resistance",
        "target_basis_value": 12.75,
        "target_basis_source": "verified_snapshot",
        "target_derived_from_risk": False,
        "previous_close": 8.0,
        "premarket_price": 10.0,
        "premarket_high": 10.1,
        "premarket_low": 9.6,
        "premarket_volume": 500_000,
        "dollar_volume": 5_000_000,
        "gap_pct": 25.0,
        "spread_pct": 0.5,
        "liquidity_tier": "high_liquidity",
        "float_shares": 8_000_000,
        "float_status": "verified",
        "float_source": "verified_snapshot",
        "catalyst_summary": "FDA clearance announced before market open",
        "catalyst_url": "https://example.test/catalyst",
        "catalyst_status": "verified",
        "catalyst_tier": "A",
        "halt_status": "clear",
        "sec_risk_status": "clear",
        "corporate_action_status": "clear",
        "alert_gate_status": "PASS",
        "manual_confirmation_required": False,
        "classification": "TRADE SETUP",
        "market_structure_observations": {
            "entry": _v5_plan_observation(
                10.0,
                "a" * 64,
                day=day,
                observation_kind="sourced_entry",
            ),
            "stop": _v5_plan_observation(
                9.0,
                "b" * 64,
                day=day,
                observation_kind="sourced_stop",
            ),
            "target": {
                **_v5_plan_observation(
                    12.75,
                    "c" * 64,
                    day=day,
                    observation_kind="prior_day_resistance",
                ),
                "target_basis_kind": "sourced_resistance",
            },
        },
    }
    from intraday_scanner.services.alpha_cycle_service import _signal_payload

    signal = _signal_payload(
        signal,
        str(signal["scan_id"]),
        str(signal["generated_at"]),
        1,
    )
    signal["raw_payload_json"] = {
        key: deepcopy(value)
        for key, value in signal.items()
        if key != "raw_payload_json"
    }
    return signal


def _v5_plan_observation(
    value: float,
    source_hash: str,
    *,
    day: str,
    observation_kind: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "raw_value": value,
        "observed_at": f"{day}T12:55:00+00:00",
        "completed_at": f"{day}T12:55:00+00:00",
        "source": "completed-market-feed",
        "source_url": "https://example.test/market",
        "source_hash": source_hash,
        "observation_kind": observation_kind,
        "derivation_policy": "identity",
        "is_complete": True,
    }


def _canonical_signal(day: str = DAY) -> dict[str, Any]:
    """Return the legacy fixture identity with a current V5 paper plan."""

    signal = {
        **_v5_signal(day),
        "signal_id": "signal-1",
        "scan_id": "scan-1",
        "alpha_signal_id": "alpha-1",
    }
    signal["raw_payload_json"] = {
        key: deepcopy(value)
        for key, value in signal.items()
        if key != "raw_payload_json"
    }
    return signal


def _run_v5_watcher_entry(
    tmp_path: Path,
    db_path: Path,
    *,
    day: str = DAY,
    requested_at: str = "10:00",
) -> dict[str, Any]:
    bar_path = tmp_path / f"watcher-entry-{day}.csv"
    with bar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "NOVA",
                "timestamp": datetime.fromisoformat(f"{day}T09:59:00")
                .replace(tzinfo=EASTERN)
                .isoformat(),
                "open": "10.05",
                "high": "10.05",
                "low": "10.05",
                "close": "10.05",
                "volume": "1000",
            }
        )
    result = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date=day,
        requested_at=requested_at,
        minute_bars=bar_path,
        dry_run=True,
        simulated_equity=100_000,
    )
    assert result["intent_stats"]["inserted"] == 1
    return result


def _persist_selected_signals(
    store: SQLiteScanStore,
    signals: list[dict[str, Any]],
    *,
    authenticated_entry: bool = False,
) -> None:
    batch_scan_id = str(signals[0].get("scan_id") or "scan-fixture")
    canonical_signals = [{**signal, "scan_id": batch_scan_id} for signal in signals]
    store.persist_historical_signals(canonical_signals)
    selections: list[dict[str, Any]] = []
    deliveries: list[dict[str, Any]] = []
    body = f"alpha-outcome-fixture:{batch_scan_id}"
    body_sha256 = hashlib.sha256(body.encode()).hexdigest()
    event_key = f"alphaops:{batch_scan_id}:alpha_morning_watch"
    for index, signal in enumerate(canonical_signals, 1):
        signal_id = str(signal["signal_id"])
        day = str(signal["market_date"])
        selection_id = f"selection-{signal_id}"
        generated_at = str(signal.get("generated_at") or "")
        selected_at = str(signal.get("_selected_at") or "") or (
            generated_at.replace("Z", "+00:00")
            if generated_at.startswith(day)
            else f"{day}T13:00:00+00:00"
        )
        strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
        common = {
            "selection_id": selection_id,
            "scan_id": str(signal.get("scan_id") or ""),
            "signal_id": signal_id,
            "market_date": day,
            "ticker": str(signal.get("ticker") or ""),
            "rank": int(signal.get("rank") or index),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cohort": "official_telegram",
            "decision": "clean_edge",
            "selected_at": selected_at,
            "event_key": event_key,
            "body_sha256": body_sha256,
            "input_hash_sha256": "8" * 64,
            "source_lineage_hash_sha256": "9" * 64,
            "delivery_identity": {
                "membership_id": f"delivery-{signal_id}",
                "channel": "telegram",
                "event_key": event_key,
                "delivery_status": "delivered",
            },
            "source_artifact_identity": f"alpha-selection:{day}:{selection_id}",
            "research_only": True,
            "broker_execution_enabled": False,
        }
        common["payload_json"] = {
            **common,
            "signal": dict(signal),
            "decision_payload": {
                "decision": "clean_edge",
                "research_only": True,
                "broker_execution_enabled": False,
            },
        }
        selections.append(common)
        delivery = {
            **common,
            "membership_id": f"delivery-{signal_id}",
            "channel": "telegram",
            "delivery_status": "delivered",
            "attempted_at": selected_at,
            "delivered_at": selected_at,
        }
        delivery["payload_json"] = {
            **delivery,
            "body": body,
            "research_only": True,
        }
        deliveries.append(delivery)
    store.persist_signal_selections(selections)
    store.persist_notification_deliveries(deliveries)
    if authenticated_entry and any(
        str(row.get("strategy_id") or "") == ALPHAOPS_V5_STRATEGY_ID
        for row in selections
    ):
        _run_v5_watcher_entry(
            store.db_path.parent,
            store.db_path,
            day=str(selections[0]["market_date"]),
        )


def _bar(
    clock: str,
    open_price: float,
    high: float | None,
    low: float,
    close: float,
    *,
    day: str = DAY,
) -> dict[str, Any]:
    observed_at = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=EASTERN)
    return {
        "timestamp": int(observed_at.timestamp()),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000.0,
    }


def _contiguous_bars(
    *,
    day: str = DAY,
    close_clock: str = "15:59",
    default: tuple[float, float | None, float, float] = (9.90, 9.95, 9.90, 9.92),
    overrides: dict[str, tuple[float, float | None, float, float]] | None = None,
) -> list[dict[str, Any]]:
    current = datetime.fromisoformat(f"{day}T09:30:00").replace(tzinfo=EASTERN)
    end = datetime.fromisoformat(f"{day}T{close_clock}:00").replace(tzinfo=EASTERN)
    selected = overrides or {}
    rows: list[dict[str, Any]] = []
    while current <= end:
        clock = current.strftime("%H:%M")
        open_price, high, low, close = selected.get(clock, default)
        rows.append(_bar(clock, open_price, high, low, close, day=day))
        current += timedelta(minutes=1)
    return rows


def _chart_payload(bars: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "chart": {
            "result": [{
                "timestamp": [row["timestamp"] for row in bars],
                "indicators": {
                    "quote": [{
                        "open": [row["open"] for row in bars],
                        "high": [row["high"] for row in bars],
                        "low": [row["low"] for row in bars],
                        "close": [row["close"] for row in bars],
                        "volume": [row["volume"] for row in bars],
                    }]
                },
            }],
            "error": None,
        }
    }
