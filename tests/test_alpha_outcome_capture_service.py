from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
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
from intraday_scanner.decisioning.contracts import StrategyDecisionReceipt, canonical_json
from intraday_scanner.errors import DataProviderError, SnapshotValidationError, StorageError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.services.alpha_cycle_service import _persist_research_radar_selections
from intraday_scanner.services.alpha_official_cohort_service import (
    validate_or_recover_official_cohort,
)
from intraday_scanner.services.alpha_outcome_capture_service import (
    capture_sourced_alpha_outcomes,
)
from intraday_scanner.services.learning_service import run_alpha_learning
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
)
from intraday_scanner.services.trade_watcher_service import run_trade_watcher
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import canonical_path_result, canonical_v6_decision

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


def _bound_rows_fetcher(rows: list[dict[str, Any]]):
    def fetch(ticker: str, *_args, **_kwargs) -> list[dict[str, Any]]:
        return [{**row, "ticker": ticker} for row in rows]

    return fetch


def _typed_strategy_contributor(
    *,
    strategy_id: str,
    strategy_version: str,
    signal_id: str,
    ticker: str,
    market_date: str,
    direction: str = "long",
) -> dict[str, Any]:
    input_payload_json = canonical_json(
        {
            "direction": direction,
            "signal_id": signal_id,
            "ticker": ticker,
        }
    )
    receipt = StrategyDecisionReceipt(
        schema_version="dawnstrike.strategy_decision_receipt.v2",
        receipt_id="",
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=ticker,
        market_date=market_date,
        decision_at=f"{market_date}T12:59:00+00:00",
        code_sha="a" * 40,
        policy_version="test-policy-v1",
        condition_results=(),
        first_blocking_failure=None,
        all_blocking_failures=(),
        disclosed_gaps=(),
        research_pick_eligible=True,
        paper_entry_eligible=False,
        pick_tier="QUALIFIED_PICK",
        base_strategy_score=1.0,
        score_adjustment=0.0,
        final_score=1.0,
        entry_reference=None,
        stop=None,
        target=None,
        reward_risk_ratio=None,
        source_identity="global-test-source",
        input_hash_sha256=hashlib.sha256(input_payload_json.encode()).hexdigest(),
        input_payload_json=input_payload_json,
    ).to_dict()
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "source_signal_id": signal_id,
        "direction": direction,
        "receipt_id": receipt["receipt_id"],
        "receipt_hash_sha256": receipt["receipt_hash_sha256"],
        "receipt_status": "COMPLETE",
        "research_pick_eligible": True,
        "paper_entry_eligible": False,
        "final_score": 1.0,
        "decision_receipt": receipt,
    }


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
        fallback_fetcher=_bound_rows_fetcher(rows),
    )

    assert result["status"] == "complete", json.dumps(
        result, sort_keys=True, indent=2, default=str
    )
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
    assert path_entry["source_observed_at"] == observation_record["payload_json"][
        "quote_observed_at"
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


def test_paper_selection_rejects_rendered_body_omitting_official_ticker(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "delivery-official-section.sqlite")
    _persist_selected_signals(store, [_v5_signal()])
    selection = deepcopy(
        store.load_signal_selections(signal_id="signal-v5-entry")[0]
    )
    delivery = deepcopy(
        store.load_notification_deliveries(
            signal_id="signal-v5-entry",
            limit=10,
        )[0]
    )
    body = (
        "OFFICIAL PAPER CANDIDATES\n"
        "- None\n"
        "\nRESEARCH WATCHLIST\n"
        "- NOVA: retained only as research"
    )
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    selection["body_sha256"] = body_hash
    selection["payload_json"]["body_sha256"] = body_hash
    delivery["body_sha256"] = body_hash
    delivery["payload_json"]["body_sha256"] = body_hash
    delivery["payload_json"]["body"] = body

    with pytest.raises(ValueError, match="official candidate section"):
        canonical_paper_selection_context(selection, delivery=delivery)


def test_paper_selection_rejects_duplicate_official_ticker_occurrence(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "delivery-duplicate-official.sqlite")
    _persist_selected_signals(store, [_v5_signal()])
    selection = deepcopy(
        store.load_signal_selections(signal_id="signal-v5-entry")[0]
    )
    delivery = deepcopy(
        store.load_notification_deliveries(
            signal_id="signal-v5-entry",
            limit=10,
        )[0]
    )
    body = (
        "PAPER PLAN QUALIFIED\n"
        "1) NOVA — Alpha 80 | fixture\n"
        "2) NOVA — Alpha 80 | duplicate\n"
        "\nRESEARCH WATCHLIST\n"
        "- None"
    )
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    selection["body_sha256"] = body_hash
    selection["payload_json"]["body_sha256"] = body_hash
    delivery["body_sha256"] = body_hash
    delivery["payload_json"]["body_sha256"] = body_hash
    delivery["payload_json"]["body"] = body

    with pytest.raises(ValueError, match="official candidate section"):
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
    assert path_entry["source_observed_at"] == f"{DAY}T14:00:30+00:00"
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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
    delivery = store.load_notification_deliveries(signal_id="signal-1")[0]
    delivery_payload = dict(delivery["payload_json"])
    delivery_payload["decision"] = "probability_fallback"
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
                    json.dumps(delivery_payload, sort_keys=True),
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
        fallback_fetcher=_bound_rows_fetcher(rows),
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


def test_no_trade_still_captures_radar_only_selection_contributors(tmp_path: Path) -> None:
    db_path = tmp_path / "no-trade-radar.sqlite"
    store = SQLiteScanStore(db_path)
    selected_at = f"{DAY}T13:00:00+00:00"
    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    body_sha256 = hashlib.sha256(b"canonical-no-trade-radar").hexdigest()
    official = {
        "selection_id": "selection-no-trade-radar",
        "scan_id": "scan-no-trade-radar",
        "signal_id": f"no_trade:{DAY}",
        "ticker": "NO_TRADE",
        "rank": 0,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": "official_telegram",
        "decision": "no_trade",
        "selected_at": selected_at,
        "event_key": "alphaops:no-trade-radar:alpha_no_trade",
        "body_sha256": body_sha256,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    official["payload_json"] = {
        **official,
        "signal": {
            "signal_id": official["signal_id"],
            "scan_id": official["scan_id"],
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
    store.persist_signal_selections([official])
    delivery = {
        **official,
        "membership_id": "delivery-no-trade-radar",
        "channel": "telegram",
        "delivery_status": "delivered",
        "attempted_at": selected_at,
        "delivered_at": selected_at,
    }
    delivery["payload_json"] = {
        **delivery,
        "body": "canonical-no-trade-radar",
        "research_only": True,
    }
    store.persist_notification_deliveries([delivery])

    radar_signal = {
        **_canonical_signal(),
        "signal_id": "radar-only-signal",
        "scan_id": "scan-radar-only",
        "episode_id": "episode:" + "c" * 32,
        "strategy_contributors": [
            _typed_strategy_contributor(
                strategy_id="radar-primary",
                strategy_version="v1",
                signal_id="radar-only-signal",
                ticker="NOVA",
                market_date=DAY,
            ),
            _typed_strategy_contributor(
                strategy_id="radar-secondary",
                strategy_version="v2",
                signal_id="radar-only-signal",
                ticker="NOVA",
                market_date=DAY,
            ),
        ],
    }
    radar_signal.update(
        {
            "strategy_decision_receipts": [
                deepcopy(row["decision_receipt"])
                for row in radar_signal["strategy_contributors"]
            ],
            "strategy_contributor_count": len(radar_signal["strategy_contributors"]),
            "strategy_contributor_ids": sorted(
                row["strategy_id"] for row in radar_signal["strategy_contributors"]
            ),
            "strongest_eligible_contributor_score": 1.0,
            "strategy_contribution_gaps": [],
            "strategy_contribution_status": "COMPLETE",
        }
    )
    for contributor in radar_signal["strategy_contributors"]:
        receipt_payload = contributor["decision_receipt"]
        store.persist_strategy_decision_receipt(
            StrategyDecisionReceipt(
                **{
                    **receipt_payload,
                    "condition_results": tuple(receipt_payload["condition_results"]),
                }
            )
        )
    slate = build_ranked_research_slate(
        [radar_signal],
        generated_at=selected_at,
        market_date=DAY,
        scan_id="scan-radar-only",
        require_safety=True,
    )
    _persist_research_radar_selections(
        store,
        scan_id="scan-radar-only",
        radar=list(slate["rows"]),
        slate=slate,
        selected_at=selected_at,
        event=NotificationEvent(
            event_key="alphaops:scan-radar-only:alpha_morning_watch",
            title="Dawnstrike Alpha Watch",
            body="Research radar: NOVA",
            channel_hint="alpha_morning_watch",
            payload={"run_id": "scan-radar-only", "signals": []},
        ),
    )
    # The reference bar contains hostile H/L excursions.  Those values must
    # not leak into post-reference path metrics; every later bar is flat.
    bars = _contiguous_bars(
        default=(100.0, 100.0, 100.0, 100.0),
        overrides={"09:30": (100.0, 200.0, 50.0, 100.0)},
    )
    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(bars),
    )
    bridges = store.load_research_episode_outcome_bridges(market_date=DAY)
    assert result["signal_count"] == 0
    assert len(bridges) == 2, json.dumps(result, sort_keys=True, indent=2, default=str)
    expected_receipt_ids = {
        row["receipt_id"] for row in radar_signal["strategy_contributors"]
    }
    assert {row["receipt_id"] for row in bridges} == expected_receipt_ids
    assert all(
        row["source_outcome_status"] == "COMPLETE_SOURCED" for row in bridges
    ), json.dumps(bridges, sort_keys=True, indent=2, default=str)
    assert all(row["outcome_status"] == "FLAT_CLOSE" for row in bridges)
    assert all(row["learning_eligible"] is True for row in bridges)
    assert all(row["selection_outcome_metrics"]["reference_price"] == 100.0 for row in bridges)
    assert all(row["selection_outcome_metrics"]["high_after_reference"] == 100.0 for row in bridges)
    assert all(row["selection_outcome_metrics"]["low_after_reference"] == 100.0 for row in bridges)
    assert all(row["selection_outcome_metrics"]["mfe_pct"] == 0.0 for row in bridges)
    assert all(row["selection_outcome_metrics"]["mae_pct"] == 0.0 for row in bridges)
    assert store.load_signal_outcomes() == []
    assert store.load_trade_intent_records(market_date=DAY) == []
    assert store.load_paper_trade_fills(market_date=DAY) == []


def test_radar_selection_without_current_bars_is_explicitly_missing() -> None:
    requested = datetime.fromisoformat(f"{DAY}T16:05:00-04:00")
    session = capture_module._session_window(DAY)
    selection = {
        "selection_id": "selection-radar-missing",
        "signal_id": "radar-missing-signal",
        "ticker": "NOVA",
        "selected_at": f"{DAY}T13:00:00+00:00",
        "payload_json": {
            "signal": {
                "signal_id": "radar-missing-signal",
                "ticker": "NOVA",
            }
        },
    }
    outcome = capture_module._derive_research_selection_outcome(
        selection,
        [],
        {},
        session=session,
        requested_at=requested,
        captured_at=f"{DAY}T20:06:00+00:00",
    )
    assert outcome["outcome_status"] in {"MISSING", "INELIGIBLE"}
    assert outcome["learning_eligible"] is False


def test_radar_outcome_requires_authenticated_canonical_source_binding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "intraday_scanner.services.research_episode_outcome_service"
        ".validate_ranked_research_slate",
        lambda slate, **_: slate,
    )
    selected_at = f"{DAY}T13:00:00+00:00"
    selection = {
        "selection_id": "selection:source-binding",
        "signal_id": "signal:source-binding",
        "ticker": "NOVA",
        "market_date": DAY,
        "cohort": "research_radar",
        "selected_at": selected_at,
        "payload_json": {
            "frozen_ranked_research_slate": {
                "slate_id": "luna-slate-" + "a" * 24,
                "content_hash_sha256": "b" * 64,
                "selection_ids": ["research-selection:source-binding"],
                "rows": [{
                    "research_selection_id": "research-selection:source-binding",
                    "signal_id": "signal:source-binding",
                    "ticker": "NOVA",
                    "market_date": DAY,
                    "episode_id": "episode:" + "c" * 32,
                }],
            },
            "signal": {
                "signal_id": "signal:source-binding",
                "ticker": "NOVA",
                "market_date": DAY,
                "research_selection_id": "research-selection:source-binding",
                "episode_id": "episode:" + "c" * 32,
            },
        },
    }
    session = capture_module.SessionWindow(
        market_date=DAY,
        opened_at=datetime.fromisoformat(f"{DAY}T09:30:00-04:00"),
        closed_at=datetime.fromisoformat(f"{DAY}T09:32:00-04:00"),
        is_trading_day=True,
        calendar={},
    )
    requested_at = datetime.fromisoformat(f"{DAY}T14:00:00+00:00")
    bars = [
        capture_module.OutcomeBar(
            observed_at=datetime.fromisoformat(f"{DAY}T09:30:00-04:00"),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
        ),
        capture_module.OutcomeBar(
            observed_at=datetime.fromisoformat(f"{DAY}T09:31:00-04:00"),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000.0,
        ),
    ]
    source_url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/NOVA?"
        "range=5d&interval=1m&includePrePost=false"
    )
    source_request = capture_module._provider_request(
        ticker="NOVA",
        source="yahoo_finance_chart",
        source_url=source_url,
        bars=bars,
        session=session,
        fetched_at=requested_at.isoformat(),
        attempt=1,
        request_contract={
            "provider": "yahoo_finance_chart",
            "ticker": "NOVA",
            "provider_symbol": "NOVA",
            "endpoint": source_url,
            "range": "5d",
            "interval": "1m",
            "include_pre_post": False,
        },
    )
    valid_evidence = capture_module._selected_source_evidence(
        source_request,
        [source_request],
        [(bars, source_request)],
    )
    valid = capture_module._derive_research_selection_outcome(
        selection,
        bars,
        valid_evidence,
        session=session,
        requested_at=requested_at,
        captured_at=requested_at.isoformat(),
    )
    assert valid["outcome_status"] == "COMPLETE_SOURCED"
    assert valid["source_authenticated"] is True
    single_bar = bars[:1]
    single_session = capture_module.SessionWindow(
        market_date=DAY,
        opened_at=session.opened_at,
        closed_at=datetime.fromisoformat(f"{DAY}T09:31:00-04:00"),
        is_trading_day=True,
        calendar={},
    )
    single_request = capture_module._provider_request(
        ticker="NOVA",
        source="yahoo_finance_chart",
        source_url=source_url,
        bars=single_bar,
        session=single_session,
        fetched_at=requested_at.isoformat(),
        attempt=1,
        request_contract={
            "provider": "yahoo_finance_chart",
            "ticker": "NOVA",
            "provider_symbol": "NOVA",
            "endpoint": source_url,
            "range": "5d",
            "interval": "1m",
            "include_pre_post": False,
        },
    )
    single_evidence = capture_module._selected_source_evidence(
        single_request,
        [single_request],
        [(single_bar, single_request)],
    )
    no_subsequent = capture_module._derive_research_selection_outcome(
        selection,
        single_bar,
        single_evidence,
        session=single_session,
        requested_at=requested_at,
        captured_at=requested_at.isoformat(),
    )
    assert no_subsequent["outcome_status"] == "MISSING"
    assert no_subsequent["learning_eligible"] is False
    assert no_subsequent["selection_outcome_metrics"]["path_status"] == (
        "NO_SUBSEQUENT_OBSERVATION"
    )
    assert no_subsequent["selection_outcome_metrics"]["mfe_pct"] is None
    assert no_subsequent["selection_outcome_metrics"]["mae_pct"] is None
    hostile = [
        {"source_artifact_identity": ""},
        {"source_url": ""},
        {"source_bar_hash_sha256": "f" * 64},
        {"source": "provider-b"},
        {"source_lineage": ["not-a-request"]},
    ]
    for mutation in hostile:
        evidence = {**valid_evidence, **mutation}
        outcome = capture_module._derive_research_selection_outcome(
            selection,
            bars,
            evidence,
            session=session,
            requested_at=requested_at,
            captured_at=requested_at.isoformat(),
        )
        assert outcome["outcome_status"] == "INELIGIBLE"
        assert outcome["learning_eligible"] is False
    for hostile_bars in (
        list(reversed(bars)),
        [capture_module.OutcomeBar(
            observed_at=bars[0].observed_at,
            open=100.0,
            high=102.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
        ), *bars[1:]],
    ):
        outcome = capture_module._derive_research_selection_outcome(
            selection,
            hostile_bars,
            valid_evidence,
            session=session,
            requested_at=requested_at,
            captured_at=requested_at.isoformat(),
        )
        assert outcome["outcome_status"] == "INELIGIBLE"
        assert outcome["learning_eligible"] is False
    for hostile_value in (0.0, -1.0, float("nan"), float("inf")):
        hostile_bars = [
            capture_module.OutcomeBar(
                observed_at=bars[0].observed_at,
                open=100.0,
                high=hostile_value,
                low=99.0,
                close=100.0,
                volume=1_000.0,
            ),
            bars[1],
        ]
        if not math.isfinite(hostile_value):
            with pytest.raises(ValueError, match="Out of range float"):
                capture_module._bars_hash(hostile_bars)
            continue
        hostile_hash = capture_module._bars_hash(hostile_bars)
        evidence = {
            **valid_evidence,
            "source_bar_hash_sha256": hostile_hash,
            "source_artifact_identity": f"artifact:provider-a:nova:{hostile_hash}",
        }
        outcome = capture_module._derive_research_selection_outcome(
            selection,
            hostile_bars,
            evidence,
            session=session,
            requested_at=requested_at,
            captured_at=requested_at.isoformat(),
        )
        assert outcome["outcome_status"] in {"MISSING", "INELIGIBLE"}
        assert outcome["learning_eligible"] is False
    assert "entry_price" not in outcome
    assert "gross_return_pct" not in outcome


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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
        fallback_fetcher=_bound_rows_fetcher(rows),
        provider_attempt_limit=2,
    )

    assert result["status"] == "partial"
    assert yahoo_calls == 6  # Two bounded reconciliation attempts for NOVA, SPY, and IWM.
    assert result["outcomes"] == []
    assert result["diagnostics"][0]["status"] == (
        "ineligible_incomplete_canonical_return_truth"
    )


def test_alpaca_outcome_request_and_rows_are_exactly_bound() -> None:
    session = capture_module._session_window(DAY)
    requested_at = session.closed_at
    rows = _contiguous_bars()
    calls: list[dict[str, Any]] = []

    def exact_fetcher(
        ticker: str,
        _config: ScannerConfig,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        calls.append({"ticker": ticker, **kwargs})
        return [{**row, "ticker": ticker} for row in rows]

    requests: list[dict[str, Any]] = []
    candidates: list[tuple[list[capture_module.OutcomeBar], dict[str, Any]]] = []
    errors: list[str] = []
    config = ScannerConfig(alpaca_data_feed="iex")
    capture_module._collect_alpaca_candidates(
        ticker="NOVA",
        config=config,
        session=session,
        requested_at=requested_at,
        captured_at=requested_at.astimezone(timezone.utc).isoformat(),
        attempt_limit=1,
        fetcher=exact_fetcher,
        requests=requests,
        candidates=candidates,
        errors=errors,
    )
    assert calls == [
        {
            "ticker": "NOVA",
            "start": "2026-08-03T13:30:00Z",
            "end": "2026-08-03T20:00:00Z",
            "timeframe": "1Min",
            "feed": "iex",
        }
    ]
    assert errors == []
    assert len(candidates) == 1
    assert requests[0]["request_contract"] == {
        "provider": "alpaca_market_data_iex",
        "ticker": "NOVA",
        "symbols": ["NOVA"],
        "endpoint": "https://data.alpaca.markets/v2/stocks/bars",
        "start": "2026-08-03T13:30:00Z",
        "end": "2026-08-03T20:00:00Z",
        "timeframe": "1Min",
        "feed": "iex",
    }

    tickerless_requests: list[dict[str, Any]] = []
    tickerless_candidates: list[
        tuple[list[capture_module.OutcomeBar], dict[str, Any]]
    ] = []
    tickerless_errors: list[str] = []
    capture_module._collect_alpaca_candidates(
        ticker="NOVA",
        config=config,
        session=session,
        requested_at=requested_at,
        captured_at=requested_at.astimezone(timezone.utc).isoformat(),
        attempt_limit=1,
        fetcher=lambda *_args, **_kwargs: rows,
        requests=tickerless_requests,
        candidates=tickerless_candidates,
        errors=tickerless_errors,
    )
    assert tickerless_candidates == []
    assert len(tickerless_requests) == 1
    assert tickerless_requests[0]["status"] == "provider_error"
    assert "explicit ticker binding" in tickerless_errors[0]


@pytest.mark.parametrize(
    ("canonical_ticker", "provider_symbol"),
    (("BRK.B", "BRK-B"), ("BF.B", "BF-B")),
)
def test_yahoo_dotted_symbol_preserves_canonical_lineage_and_binds_provider_symbol(
    canonical_ticker: str,
    provider_symbol: str,
) -> None:
    session = capture_module._session_window(DAY)
    rows = _contiguous_bars()
    calls: list[str] = []

    def exact_fetcher(symbol: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(symbol)
        return _chart_payload(rows)

    requests: list[dict[str, Any]] = []
    candidates: list[tuple[list[capture_module.OutcomeBar], dict[str, Any]]] = []
    errors: list[str] = []
    capture_module._collect_yahoo_candidates(
        ticker=canonical_ticker,
        config=ScannerConfig(),
        session=session,
        requested_at=session.closed_at,
        captured_at=session.closed_at.astimezone(timezone.utc).isoformat(),
        attempt_limit=1,
        fetcher=exact_fetcher,
        requests=requests,
        candidates=candidates,
        errors=errors,
    )

    expected_url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}?"
        "range=5d&interval=1m&includePrePost=false"
    )
    assert calls == [provider_symbol]
    assert errors == []
    assert len(candidates) == 1
    assert requests[0]["ticker"] == canonical_ticker
    assert requests[0]["source_url"] == expected_url
    assert requests[0]["source_artifact_identity"].startswith(
        f"market-bars:yahoo_finance_chart:{canonical_ticker}:{DAY}:1m:"
    )
    assert requests[0]["request_contract"] == {
        "provider": "yahoo_finance_chart",
        "ticker": canonical_ticker,
        "provider_symbol": provider_symbol,
        "endpoint": expected_url,
        "range": "5d",
        "interval": "1m",
        "include_pre_post": False,
    }


def test_independent_provider_disagreement_is_explicit_and_fail_closed() -> None:
    session = capture_module._session_window(DAY)
    yahoo_rows = _contiguous_bars(default=(100.0, 100.0, 100.0, 100.0))
    alpaca_rows = _contiguous_bars(default=(110.0, 110.0, 110.0, 110.0))
    yahoo_bars = capture_module._regular_session_bars_from_rows(
        "NOVA",
        [{**row, "ticker": "NOVA"} for row in yahoo_rows],
        session=session,
        requested_at=session.closed_at,
    )
    alpaca_bars = capture_module._regular_session_bars_from_rows(
        "NOVA",
        [{**row, "ticker": "NOVA"} for row in alpaca_rows],
        session=session,
        requested_at=session.closed_at,
    )
    yahoo_url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/NOVA?"
        "range=5d&interval=1m&includePrePost=false"
    )
    alpaca_url = "https://data.alpaca.markets/v2/stocks/bars"
    yahoo_request = capture_module._provider_request(
        ticker="NOVA",
        source="yahoo_finance_chart",
        source_url=yahoo_url,
        bars=yahoo_bars,
        session=session,
        fetched_at="2026-08-03T20:00:00Z",
        attempt=1,
        request_contract={
            "provider": "yahoo_finance_chart",
            "ticker": "NOVA",
            "provider_symbol": "NOVA",
            "endpoint": yahoo_url,
            "range": "5d",
            "interval": "1m",
            "include_pre_post": False,
        },
    )
    alpaca_request = capture_module._provider_request(
        ticker="NOVA",
        source="alpaca_market_data_iex",
        source_url=alpaca_url,
        bars=alpaca_bars,
        session=session,
        fetched_at="2026-08-03T20:00:00Z",
        attempt=1,
        request_contract={
            "provider": "alpaca_market_data_iex",
            "ticker": "NOVA",
            "symbols": ["NOVA"],
            "endpoint": alpaca_url,
            "start": "2026-08-03T13:30:00Z",
            "end": "2026-08-03T20:00:00Z",
            "timeframe": "1Min",
            "feed": "iex",
        },
    )
    evidence = capture_module._selected_source_evidence(
        yahoo_request,
        [yahoo_request, alpaca_request],
        [(yahoo_bars, yahoo_request), (alpaca_bars, alpaca_request)],
    )
    assert evidence["independent_reconciliation_status"] == "DISAGREEMENT"
    assert evidence["independent_reconciliation"]["agreement"] is False
    assert evidence["source_conflict"] is True


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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
        [{**_v5_signal(), "_selected_at": f"{DAY}T10:00:00"}],
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


def test_all_eligible_distinct_ticker_signals_are_captured(tmp_path: Path) -> None:
    db_path = tmp_path / "multiple-signals.sqlite"
    store = SQLiteScanStore(db_path)
    second = {
        **_signal(),
        "signal_id": "signal-2",
        "scan_id": "scan-2",
        "alpha_signal_id": "alpha-2",
        "ticker": "MSFT",
        "company": "Microsoft Corp.",
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


def test_signal_children_require_governed_parent_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "foreign-key.sqlite"
    store = SQLiteScanStore(db_path)
    orphan_outcome = {
        "signal_id": "missing-historical-signal",
        "market_date": DAY,
        "ticker": "NOVA",
        "outcome_source": "test",
        "imported_at": f"{DAY}T20:05:00Z",
        "outcome_status": "complete_sourced",
    }
    orphan_event = {
        "event_id": "orphan-event",
        "signal_id": "missing-historical-signal",
        "event_type": "OUTCOME_CAPTURED",
        "event_timestamp": f"{DAY}T20:05:00Z",
        "source": "test",
    }

    with pytest.raises(StorageError, match="Signal child parent validation failed"):
        store.persist_signal_outcomes([orphan_outcome])
    with pytest.raises(StorageError, match="Signal child parent validation failed"):
        store.persist_signal_events([orphan_event])

    _persist_selected_signals(store, [_signal()])
    valid_v5_outcome = {**orphan_outcome, "signal_id": "signal-1"}
    for mutation in ({"market_date": "2026-08-04"}, {"ticker": "OTHER"}):
        with pytest.raises(StorageError, match="Signal child parent validation failed"):
            store.persist_signal_outcomes([{**valid_v5_outcome, **mutation}])

    v6_decision = canonical_v6_decision("shadow-parent")
    store.persist_alpha_v6_decisions([v6_decision])
    valid_v6_outcome = {
        **orphan_outcome,
        "signal_id": v6_decision["shadow_signal_id"],
        "market_date": v6_decision["market_date"],
        "ticker": v6_decision["ticker"],
    }
    assert store.persist_signal_outcomes([valid_v6_outcome]) == {"inserted": 1, "skipped": 0}
    valid_v6_event = {**orphan_event, "signal_id": v6_decision["shadow_signal_id"]}
    assert store.persist_signal_events([valid_v6_event]) == {"inserted": 1, "skipped": 0}
    for mutation in ({"market_date": "2026-08-04"}, {"ticker": "OTHER"}):
        with pytest.raises(StorageError, match="Signal child parent validation failed"):
            store.persist_signal_outcomes([{**valid_v6_outcome, **mutation}])

    atomic_store = SQLiteScanStore(tmp_path / "atomic-parent.sqlite")
    _persist_selected_signals(atomic_store, [_signal()])
    with pytest.raises(StorageError, match="Signal child parent validation failed"):
        atomic_store.persist_signal_outcomes_with_events(
            [valid_v5_outcome], [orphan_event]
        )

    # The atomic path must roll back the valid child row when its sibling is
    # rejected, so no partial outcome/evidence can survive the failure.
    assert atomic_store.load_signal_outcomes() == []
    assert atomic_store.load_signal_events() == []


def test_signal_child_parent_identity_rejects_conflicting_v5_and_v6_parents(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "ambiguous-parent.sqlite")
    historical = {**_signal(), "signal_id": "shared-signal"}
    store.persist_historical_signals([historical])
    decision = canonical_v6_decision("ambiguous-parent")
    decision.update(
        {
            "shadow_signal_id": "shared-signal",
            "market_date": DAY,
            "ticker": "NOVA",
        }
    )
    store.persist_alpha_v6_decisions([decision])
    outcome = {
        "signal_id": "shared-signal",
        "market_date": DAY,
        "ticker": "NOVA",
        "outcome_source": "test",
        "imported_at": f"{DAY}T20:05:00Z",
        "outcome_status": "complete_sourced",
    }

    with pytest.raises(StorageError, match="Signal child parent validation failed"):
        store.persist_signal_outcomes([outcome])
    with pytest.raises(StorageError, match="Signal child parent validation failed"):
        store.persist_signal_events(
            [{"event_id": "ambiguous-event", "signal_id": "shared-signal"}]
        )


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
        fallback_fetcher=_bound_rows_fetcher(rows),
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
        fallback_fetcher=_bound_rows_fetcher(
            _contiguous_bars(
                overrides={"10:01": (12.50, 13.00, 12.40, 12.80)}
            )
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
        "ticker": "MSFT",
        "company": "Microsoft Corp.",
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
    from intraday_scanner.services.alpha_cycle_service import (
        _attach_authenticated_alpaca_structure,
        _signal_payload,
    )

    decision_at = datetime.fromisoformat(
        str(signal["generated_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    observed_at = decision_at - timedelta(minutes=5)
    completed_at = observed_at + timedelta(minutes=1)
    prior_day = (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat()
    source = "alpaca_market_data_iex"
    source_url = "https://data.alpaca.markets/v2/stocks/bars"
    premarket_raw = {
        "ticker": "NOVA",
        "feed": "iex",
        "requested_at": decision_at.isoformat(),
        "bars": [
            {
                "ticker": "NOVA",
                "timestamp": observed_at.isoformat(),
                "high": 10.0,
                "low": 9.0,
                "close": 9.5,
                "volume": 500_000,
            }
        ],
    }
    premarket_raw_json = json.dumps(
        premarket_raw, sort_keys=True, separators=(",", ":")
    )
    prior_raw = {
        "ticker": "NOVA",
        "timestamp": f"{prior_day}T00:00:00+00:00",
        "high": 12.75,
        "bar": {"t": f"{prior_day}T00:00:00Z", "h": 12.75},
    }
    prior_raw_json = json.dumps(prior_raw, sort_keys=True, separators=(",", ":"))
    observation_payload = {
        "ticker": "NOVA",
        "status": "verified",
        "premarket_high": 10.0,
        "premarket_low": 9.0,
        "previous_close": 8.0,
        "latest_price": 9.5,
        "premarket_volume": 500_000,
        "observed_at": observed_at.isoformat(),
        "bar_completed_at": completed_at.isoformat(),
        "is_complete": True,
        "bar_count": 1,
        "age_seconds": int((decision_at - completed_at).total_seconds()),
        "source": source,
        "source_url": source_url,
        "failure_reason": "",
        "prior_daily_high": 12.75,
        "prior_daily_high_observed_at": f"{prior_day}T00:00:00+00:00",
        "prior_daily_high_completed_at": f"{day}T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": source,
        "prior_daily_high_source_url": source_url,
        "prior_daily_high_source_hash": hashlib.sha256(
            prior_raw_json.encode()
        ).hexdigest(),
        "prior_daily_high_raw_payload_json": prior_raw_json,
        "premarket_raw_payload_json": premarket_raw_json,
        "premarket_source_hash_sha256": hashlib.sha256(
            premarket_raw_json.encode()
        ).hexdigest(),
    }
    observation_payload_json = json.dumps(
        observation_payload, sort_keys=True, separators=(",", ":")
    )
    signal.update(
        {
            "premarket_high": 10.0,
            "premarket_low": 9.0,
            "premarket_range_source": source,
            "premarket_range_source_url": source_url,
            "enrichment_primary_source": source,
            "enrichment_status": "verified",
            "enrichment_is_complete": True,
            "enrichment_was_fallback": False,
            "freshness_status": "FRESH",
            "input_status": "VERIFIED",
            "evidence_status": "VERIFIED",
            "enrichment_observed_at": observed_at.isoformat(),
            "enrichment_bar_completed_at": completed_at.isoformat(),
            "enrichment_observation_sha256": hashlib.sha256(
                observation_payload_json.encode()
            ).hexdigest(),
            "enrichment_observation_payload_json": observation_payload_json,
            "premarket_raw_payload_json": premarket_raw_json,
            "premarket_source_hash_sha256": hashlib.sha256(
                premarket_raw_json.encode()
            ).hexdigest(),
            "prior_daily_high": 12.75,
            "prior_daily_high_observed_at": f"{prior_day}T00:00:00+00:00",
            "prior_daily_high_completed_at": f"{day}T00:00:00+00:00",
            "prior_daily_high_completion_semantics": "availability_boundary",
            "prior_daily_high_source": source,
            "prior_daily_high_source_url": source_url,
            "prior_daily_high_source_hash": hashlib.sha256(
                prior_raw_json.encode()
            ).hexdigest(),
            "prior_daily_high_raw_payload_json": prior_raw_json,
        }
    )
    signal = _attach_authenticated_alpaca_structure(
        signal, decision_at=decision_at.isoformat()
    )

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
    del tmp_path
    requested = datetime.fromisoformat(f"{day}T{requested_at}").replace(tzinfo=EASTERN)
    quote_at = requested.astimezone(timezone.utc).isoformat()
    bar_at = (
        requested.replace(second=0, microsecond=0) - timedelta(minutes=1)
    ).astimezone(timezone.utc).isoformat()

    class AuthenticatedFixtureAlpaca:
        def __init__(self, _config):
            pass

        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            del start, end, config
            return [
                {
                    "ticker": symbols[0],
                    "timestamp": bar_at,
                    "open": 10.05,
                    "high": 10.05,
                    "low": 10.05,
                    "close": 10.05,
                    "volume": 1000,
                }
            ]

        def get_latest_quotes(self, symbols, config):
            del config
            raw = {"t": quote_at, "bp": 10.04, "ap": 10.05}
            raw_json = json.dumps(
                {"ticker": symbols[0], "quote": raw},
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                symbols[0]: {
                    "ticker": symbols[0],
                    "timestamp": quote_at,
                    "bid": 10.04,
                    "ask": 10.05,
                    "source": "alpaca_market_data_iex",
                    "raw_payload_json": raw_json,
                    "source_hash_sha256": hashlib.sha256(raw_json.encode()).hexdigest(),
                }
            }

    with patch(
        "intraday_scanner.services.price_observation_service.AlpacaProvider",
        AuthenticatedFixtureAlpaca,
    ):
        result = run_trade_watcher(
            db_path=db_path,
            source="alpaca",
            market_date=day,
            requested_at=requested_at,
            dry_run=True,
            simulated_equity=100_000,
        )
    assert result["intent_stats"]["inserted"] == 1, json.dumps(
        result, sort_keys=True, indent=2, default=str
    )
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
    tickers = list(
        dict.fromkeys(
            str(signal.get("ticker") or "").upper()
            for signal in canonical_signals
        )
    )
    body = "\n".join(
        [
            "OFFICIAL PAPER CANDIDATES",
            *[
                f"{index}) {ticker} — Alpha 80 | fixture"
                for index, ticker in enumerate(tickers, 1)
            ],
            "",
            "RESEARCH WATCHLIST",
            "- None",
        ]
    )
    body_sha256 = hashlib.sha256(body.encode()).hexdigest()
    event_key = f"alphaops:{batch_scan_id}:alpha_morning_watch"
    slate_day = str(canonical_signals[0]["market_date"])
    slate_generated_at = str(canonical_signals[0].get("generated_at") or "")
    if not slate_generated_at.startswith(slate_day):
        slate_generated_at = f"{slate_day}T13:00:00+00:00"
    strict_slate = authenticated_entry or any(
        str(row.get("strategy_id") or "") == ALPHAOPS_V5_STRATEGY_ID
        for row in canonical_signals
    )
    frozen_slate = build_ranked_research_slate(
        canonical_signals,
        generated_at=slate_generated_at,
        market_date=slate_day,
        scan_id=batch_scan_id,
        require_safety=strict_slate,
    )
    frozen_by_signal = {
        str(row.get("signal_id") or ""): row for row in frozen_slate["rows"]
    }
    frozen_lineage = {
        "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
        "slate_id": frozen_slate["slate_id"],
        "slate_content_hash_sha256": frozen_slate["content_hash_sha256"],
        "frozen_source_scan_id": batch_scan_id,
        "current_scan_id": batch_scan_id,
        "reuse_status": "CURRENT_SCAN",
    }
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
            "source_scan_id": batch_scan_id,
            "signal": dict(frozen_by_signal[signal_id]),
            "frozen_ranked_research_slate": frozen_slate,
            "frozen_slate_lineage": frozen_lineage,
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
