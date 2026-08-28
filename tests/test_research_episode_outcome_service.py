import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner import cli as scanner_cli
from intraday_scanner.decisioning.contracts import (
    ConditionResult,
    StrategyDecisionReceipt,
    canonical_json,
)
from intraday_scanner.errors import SnapshotValidationError, StorageError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.performance.strategy_miss_attribution import (
    load_strategy_learning_database_snapshot_readonly,
)
from intraday_scanner.services import alpha_outcome_capture_service as capture
from intraday_scanner.services import research_episode_outcome_service as bridge
from intraday_scanner.services.alpha_cycle_service import _persist_research_radar_selections
from intraday_scanner.services.daily_strategy_learning_service import (
    _aggregate_decision_receipts,
    _apply_research_episode_outcomes,
    _bridge_learning_summary,
    _validated_research_bridges,
)
from intraday_scanner.services.luna_research_slate_service import build_ranked_research_slate
from intraday_scanner.services.premarket_enrichment_service import (
    _canonical_observation_payload,
    observation_from_alpaca_bars,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _contributor(
    strategy_id: str,
    strategy_version: str,
    *,
    direction: str = "long",
    include_signal: bool = True,
    source_identity: str = "global-test-source",
) -> dict[str, object]:
    input_payload = {
        "direction": direction,
        "ticker": "NOVA",
    }
    if include_signal:
        input_payload["signal_id"] = "signal:nova"
    input_payload_json = canonical_json(input_payload)
    receipt = StrategyDecisionReceipt(
        schema_version="dawnstrike.strategy_decision_receipt.v2",
        receipt_id="",
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol="NOVA",
        market_date="2026-08-28",
        decision_at="2026-08-28T13:59:00+00:00",
        code_sha="test-sha",
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
        source_identity=source_identity,
        input_hash_sha256=hashlib.sha256(input_payload_json.encode()).hexdigest(),
        input_payload_json=input_payload_json,
    ).to_dict()
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "source_signal_id": "signal:nova",
        "direction": direction,
        "receipt_id": receipt["receipt_id"],
        "receipt_hash_sha256": receipt["receipt_hash_sha256"],
        "receipt_status": "COMPLETE",
        "decision_receipt": receipt,
    }


def _v1_contributor() -> dict[str, object]:
    receipt = StrategyDecisionReceipt(
        schema_version="dawnstrike.strategy_decision_receipt.v1",
        receipt_id="",
        strategy_id="legacy-v1",
        strategy_version="v1",
        symbol="NOVA",
        market_date="2026-08-28",
        decision_at="2026-08-28T13:59:00+00:00",
        code_sha="test-sha",
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
        input_hash_sha256="a" * 64,
    ).to_dict()
    return {
        "strategy_id": "legacy-v1",
        "strategy_version": "v1",
        "source_signal_id": "signal:nova",
        "direction": "long",
        "receipt_id": receipt["receipt_id"],
        "receipt_hash_sha256": receipt["receipt_hash_sha256"],
        "receipt_status": "COMPLETE",
        "decision_receipt": receipt,
    }


def _with_contributors(
    selection: dict[str, object], contributors: list[dict[str, object]]
) -> dict[str, object]:
    updated = deepcopy(selection)
    payload = updated["payload_json"]
    assert isinstance(payload, dict)
    signal = payload["signal"]
    slate = payload["frozen_ranked_research_slate"]
    assert isinstance(signal, dict)
    assert isinstance(slate, dict)
    signal["strategy_contributors"] = deepcopy(contributors)
    rows = slate["rows"]
    assert isinstance(rows, list)
    rows[0]["strategy_contributors"] = deepcopy(contributors)
    return updated


def _persist_contributor_receipts(
    store: SQLiteScanStore,
    selection: dict[str, object],
) -> None:
    payload = selection["payload_json"]
    assert isinstance(payload, dict)
    signal = payload["signal"]
    assert isinstance(signal, dict)
    contributors = signal["strategy_contributors"]
    assert isinstance(contributors, list)
    for contributor in contributors:
        assert isinstance(contributor, dict)
        receipt_payload = contributor["decision_receipt"]
        assert isinstance(receipt_payload, dict)
        receipt = StrategyDecisionReceipt(
            **{
                **receipt_payload,
                "condition_results": tuple(
                    ConditionResult(**dict(item))
                    for item in receipt_payload["condition_results"]
                ),
            }
        )
        store.persist_strategy_decision_receipt(receipt)


def _insert_research_bridge_directly(
    database_path: Path,
    row: dict[str, object],
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO research_episode_outcome_bridges
            (bridge_id, bridge_hash_sha256, logical_key, selection_id, slate_id,
             slate_content_hash_sha256, episode_id, ticker, market_date,
             selected_at, strategy_id, strategy_version, receipt_id,
             receipt_hash_sha256, outcome_status, learning_eligible,
             source_observation_id, source_observation_hash_sha256,
             source_path_id, source_path_hash_sha256, source_cutoff,
             outcome_artifact_id, outcome_artifact_hash_sha256,
             payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["bridge_id"],
                row["bridge_hash_sha256"],
                row["logical_key"],
                row["selection_id"],
                row["slate_id"],
                row["slate_content_hash_sha256"],
                row["episode_id"],
                row["ticker"],
                row["market_date"],
                row["selected_at"],
                row["strategy_id"],
                row["strategy_version"],
                row["receipt_id"],
                row["receipt_hash_sha256"],
                row["outcome_status"],
                int(row["learning_eligible"] is True),
                row.get("source_observation_id"),
                row.get("source_observation_hash_sha256"),
                row.get("source_path_id"),
                row.get("source_path_hash_sha256"),
                row.get("source_cutoff"),
                row.get("outcome_artifact_id"),
                row.get("outcome_artifact_hash_sha256"),
                json.dumps(row, sort_keys=True, separators=(",", ":")),
                row["created_at"],
            ),
        )


def _selection() -> dict[str, object]:
    contributors = [_contributor("primary", "v1"), _contributor("secondary", "v2")]
    return {
        "selection_id": "selection:nova",
        "signal_id": "signal:nova",
        "ticker": "NOVA",
        "market_date": "2026-08-28",
        "cohort": "research_radar",
        "strategy_id": "research_radar",
        "strategy_version": "v1",
        "selected_at": "2026-08-28T14:00:00+00:00",
        "episode_id": "episode:" + "a" * 32,
        "payload_json": {
            "frozen_ranked_research_slate": {
                "slate_id": "luna-slate-" + "b" * 24,
                "content_hash_sha256": "c" * 64,
                "selection_ids": ["research-selection:nova"],
                "rows": [{
                    "research_selection_id": "research-selection:nova",
                    "signal_id": "signal:nova",
                    "ticker": "NOVA",
                    "market_date": "2026-08-28",
                    "episode_id": "episode:" + "a" * 32,
                    "strategy_contributors": contributors,
                }],
            },
            "signal": {
                "signal_id": "signal:nova",
                "ticker": "NOVA",
                "research_selection_id": "research-selection:nova",
                "episode_id": "episode:" + "a" * 32,
                # Production _persist_research_radar_selections stores this
                # exact nested contributor list under payload_json.signal.
                "strategy_contributors": contributors,
            },
        },
    }


def _outcome() -> dict[str, object]:
    return {
        "selection_id": "selection:nova",
        "signal_id": "signal:nova",
        "market_date": "2026-08-28",
        "outcome_status": "WIN",
        "source_authenticated": True,
        "automatic_sourced_data": True,
        "requested_at": "2026-08-28T20:00:00+00:00",
        "source_observation_hash_sha256": "f" * 64,
        "replay_receipt_hash_sha256": "1" * 64,
        "source_bar_hash_sha256": "2" * 64,
        "path_replay_id": "path-v2-nova",
        "source_last_bar_at": "2026-08-28T19:59:00+00:00",
        "source_coverage_complete": True,
        "coverage_maximum_gap_seconds": 60,
        "coverage_allowed_gap_seconds": 60,
        "learning_eligible": True,
        "entry_price": 999.0,
        "gross_return_pct": 99.0,
    }


def _legacy_complete_selection_outcome() -> dict[str, object]:
    provider = "yahoo_finance_chart"
    source_url = "https://query1.finance.yahoo.com/v8/finance/chart/NOVA?range=5d&interval=1m&includePrePost=false"
    bar_hash = "2" * 64
    lineage = [{"source": provider, "source_url": source_url, "request": "GET /chart"}]
    binding = {
        "provider": provider,
        "source_url": source_url,
        "source_artifact_identity": f"market-bars:{provider}:NOVA:2026-08-28:1m:{bar_hash}",
        "source_bar_hash_sha256": bar_hash,
        "source_lineage": lineage,
        "source_cutoff": "2026-08-28T20:00:00+00:00",
        "source_request_hash_sha256": bridge._digest_list(lineage),
    }
    metrics = {
        "reference_at": "2026-08-28T14:01:00Z",
        "reference_price": 100.0,
        "close_at": "2026-08-28T14:02:00Z",
        "close_price": 101.0,
        "high_after_reference": 101.0,
        "low_after_reference": 100.0,
        "mfe_pct": 1.0,
        "mae_pct": 0.0,
        "path_status": "POSITIVE_CLOSE",
        "bar_count": 1,
    }
    observation_payload = {
        "ticker": "NOVA",
        "market_date": "2026-08-28",
        "observed_at": metrics["reference_at"],
        "open": 100.0,
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
        "volume": 1.0,
    }
    path_id = f"selection-path:selection:nova:{bar_hash[:24]}"
    path_payload = {
        "path_id": path_id,
        "metrics": metrics,
        "source_bar_hash_sha256": bar_hash,
    }
    metric_body = {
        "selection_id": "selection:nova",
        "signal_id": "signal:nova",
        "ticker": "NOVA",
        "market_date": "2026-08-28",
        "source_bar_hash_sha256": bar_hash,
        "source_binding": binding,
        "metrics": metrics,
    }
    metric_hash = bridge._digest(metric_body)
    return {
        "selection_id": "selection:nova",
        "signal_id": "signal:nova",
        "market_date": "2026-08-28",
        "selected_at": "2026-08-28T14:00:00+00:00",
        "outcome_status": "COMPLETE_SOURCED",
        "source_authenticated": True,
        "automatic_sourced_data": True,
        "source_provider": provider,
        "source": provider,
        "source_url": source_url,
        "source_artifact_identity": binding["source_artifact_identity"],
        "source_bar_hash_sha256": bar_hash,
        "source_bar_interval": "1m",
        "source_binding": binding,
        "source_lineage": lineage,
        "source_cutoff": binding["source_cutoff"],
        "source_coverage_complete": True,
        "coverage_maximum_gap_seconds": 0,
        "coverage_allowed_gap_seconds": 60,
        "capture_model_version": "alphaops-sourced-outcome-v3",
        "capture_mode": "automatic_sourced_selection_observation",
        "no_lookahead": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "source_observation_id": "selection-observation:selection:nova:2026-08-28T14:01:00Z",
        "source_observation_hash_sha256": bridge._digest(observation_payload),
        "source_observation_payload": observation_payload,
        "source_path_id": path_id,
        "source_path_hash_sha256": bridge._digest(path_payload),
        "source_path_payload": path_payload,
        "outcome_artifact_id": f"selection-outcome:selection:nova:{metric_hash[:24]}",
        "outcome_artifact_hash_sha256": metric_hash,
        "selection_outcome_metrics": metrics,
        "learning_eligible": True,
    }


def _complete_selection_outcome(*, post_price: float = 101.0) -> dict[str, object]:
    eastern = capture.EASTERN
    opened = datetime.fromisoformat("2026-08-28T09:30:00").replace(tzinfo=eastern)
    closed = datetime.fromisoformat("2026-08-28T16:00:00").replace(tzinfo=eastern)
    session = capture.SessionWindow(
        market_date="2026-08-28",
        opened_at=opened,
        closed_at=closed,
        is_trading_day=True,
        calendar={},
    )
    bars = []
    current = opened
    while current < closed:
        price = (
            100.0
            if current
            < datetime.fromisoformat("2026-08-28T10:01:00").replace(tzinfo=eastern)
            else post_price
        )
        bars.append(
            capture.OutcomeBar(
                observed_at=current,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1.0,
            )
        )
        current += timedelta(minutes=1)
    provider = "yahoo_finance_chart"
    source_url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/NOVA?"
        "range=5d&interval=1m&includePrePost=false"
    )
    request = capture._provider_request(
        ticker="NOVA",
        source=provider,
        source_url=source_url,
        bars=bars,
        session=session,
        fetched_at=closed.astimezone(capture.UTC).isoformat(),
        attempt=1,
        request_contract={
            "provider": provider,
            "ticker": "NOVA",
            "provider_symbol": "NOVA",
            "endpoint": source_url,
            "range": "5d",
            "interval": "1m",
            "include_pre_post": False,
        },
    )
    evidence = capture._selected_source_evidence(request, [request], [(bars, request)])
    return capture._derive_research_selection_outcome(
        _selection(),
        bars,
        evidence,
        session=session,
        requested_at=closed.astimezone(capture.UTC),
        captured_at=closed.astimezone(capture.UTC).isoformat(),
    )


def test_bridge_binds_every_contributor_and_scrubs_trade_fields(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert [row["receipt_id"] for row in rows] == [
        _contributor("primary", "v1")["receipt_id"],
        _contributor("secondary", "v2")["receipt_id"],
    ]
    assert all(row["outcome_status"] == "INELIGIBLE" for row in rows)
    assert all("trade outcome" in row["outcome_reason"] for row in rows)
    assert all("entry_price" not in row and "gross_return_pct" not in row for row in rows)
    assert all(row["research_only"] is True for row in rows)
    assert all(row["broker_execution_enabled"] is False for row in rows)


def test_nested_json_envelopes_preserve_primary_and_secondary_receipts(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    payload = dict(selection["payload_json"])
    signal = dict(payload["signal"])
    signal["strategy_contributors"] = json.dumps(
        signal["strategy_contributors"], sort_keys=True
    )
    payload["signal"] = json.dumps(signal, sort_keys=True)
    selection["payload_json"] = json.dumps(payload, sort_keys=True)
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert [
        (row["strategy_id"], row["receipt_id"], row["receipt_hash_sha256"])
        for row in rows
    ] == [
        (
            "primary",
            _contributor("primary", "v1")["receipt_id"],
            _contributor("primary", "v1")["receipt_hash_sha256"],
        ),
        (
            "secondary",
            _contributor("secondary", "v2")["receipt_id"],
            _contributor("secondary", "v2")["receipt_hash_sha256"],
        ),
    ]


def test_production_persist_shape_binds_nested_contributors_once(
    tmp_path: Path,
) -> None:
    selected_at = "2026-08-28T13:00:00+00:00"
    cycle_at = datetime.fromisoformat(selected_at)
    observation = observation_from_alpaca_bars(
        "NOVA",
        [
            {
                "ticker": "NOVA",
                "timestamp": (cycle_at - timedelta(minutes=2)).isoformat(),
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000,
            }
        ],
        previous_close=9.5,
        requested_at=cycle_at,
        max_age_seconds=600,
        feed="iex",
    )
    observation_hash, observation_payload = _canonical_observation_payload(observation)
    signal = {
        "signal_id": "signal:production-nova",
        "ticker": "NOVA",
        "episode_id": "episode:" + "f" * 32,
        "market_date": "2026-08-28",
        "universe_lane": "mover",
        "evidence_lane": "mover",
        "source_count": 1,
        "source_quality_status": "VERIFIED",
        "freshness_status": "FRESH",
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "input_status": "VERIFIED",
        "evidence_status": "VERIFIED",
        "enrichment_observation_sha256": observation_hash,
        "enrichment_observation_payload_json": observation_payload,
        "strategy_contributors": [
            {
                "strategy_id": "primary",
                "strategy_version": "v1",
                "receipt_id": "sdr-primary",
                "receipt_hash_sha256": "d" * 64,
            },
            {
                "strategy_id": "secondary",
                "strategy_version": "v2",
                "receipt_id": "sdr-secondary",
                "receipt_hash_sha256": "e" * 64,
            },
        ],
    }
    slate = build_ranked_research_slate(
        [signal],
        generated_at=selected_at,
        market_date="2026-08-28",
        scan_id="scan-production",
        require_safety=True,
    )
    store = SQLiteScanStore(tmp_path / "production-shape.sqlite")
    event = NotificationEvent(
        event_key="alphaops:scan-production:alpha_morning_watch",
        title="Dawnstrike Alpha Watch",
        body="Research radar: NOVA",
        channel_hint="alpha_morning_watch",
        payload={"run_id": "scan-production", "signals": []},
    )
    _persist_research_radar_selections(
        store,
        scan_id="scan-production",
        radar=list(slate["rows"]),
        slate=slate,
        selected_at=selected_at,
        event=event,
    )
    persisted = store.load_signal_selections(cohort="research_radar")
    assert len(persisted) == 1
    nested = persisted[0]["payload_json"]["signal"]["strategy_contributors"]
    assert [item["receipt_id"] for item in nested] == ["sdr-primary", "sdr-secondary"]
    outcome = {
        **_outcome(),
        "selection_id": persisted[0]["selection_id"],
        "signal_id": persisted[0]["signal_id"],
    }
    rows = bridge.build_research_episode_outcome_bridges(
        persisted,
        [outcome],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert [row["receipt_id"] for row in rows] == ["sdr-primary", "sdr-secondary"]
    assert all(row["learning_eligible"] is False for row in rows)


def test_stripped_nested_contributors_are_ineligible_and_do_not_inherit(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    del selection["payload_json"]["signal"]["strategy_contributors"]
    with pytest.raises(SnapshotValidationError, match="contributor receipts"):
        bridge.build_research_episode_outcome_bridges(
            [selection],
            [_outcome()],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )


def test_mutated_nested_receipt_hash_cannot_join_learning(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    selection["payload_json"]["signal"]["strategy_contributors"][0][
        "receipt_hash_sha256"
    ] = "9" * 64
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert rows[0]["receipt_status"] == "INVALID"
    assert rows[0]["learning_eligible"] is False
    receipts = (
        {
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "strategy_id": "primary",
            "strategy_version": "v1",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
            "outcome_state": "MISSING",
        },
    )
    assert _apply_research_episode_outcomes(receipts, []) == receipts


@pytest.mark.parametrize("case", ("minimal", "v1", "source_identity", "status"))
def test_only_exact_complete_v2_contributor_receipts_can_join(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    if case == "minimal":
        minimal_body = {
            "strategy_id": "minimal",
            "strategy_version": "v1",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
            "input_payload_json": canonical_json(
                {"direction": "long", "signal_id": "signal:nova", "ticker": "NOVA"}
            ),
        }
        receipt_hash = hashlib.sha256(canonical_json(minimal_body).encode()).hexdigest()
        contributor = {
            **minimal_body,
            "source_signal_id": "signal:nova",
            "direction": "long",
            "receipt_id": "sdr-" + receipt_hash[:24],
            "receipt_hash_sha256": receipt_hash,
            "receipt_status": "COMPLETE",
            "decision_receipt": minimal_body,
        }
    elif case == "v1":
        contributor = _v1_contributor()
    elif case == "source_identity":
        contributor = _contributor(
            "source-identity-shortcut",
            "v2",
            include_signal=False,
            source_identity="signal:nova",
        )
    else:
        contributor = _contributor("partial-status", "v2")
        contributor["receipt_status"] = "PARTIAL"
    selection = _with_contributors(_selection(), [contributor])
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["receipt_status"] == "INVALID"
    assert rows[0]["outcome_status"] == "INELIGIBLE"
    assert rows[0]["learning_eligible"] is False
    assert "contributor receipt" in rows[0]["outcome_reason"]


def test_mixed_long_short_contributors_use_receipt_bound_direction(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    contributors = [
        _contributor("long-strategy", "v1", direction="long"),
        _contributor("failed_breakout_reversal_short", "v1", direction="short"),
    ]
    selection = _with_contributors(_selection(), contributors)
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_complete_selection_outcome(post_price=99.0)],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    assert len(rows) == 2
    by_strategy = {row["strategy_id"]: row for row in rows}
    long_metrics = by_strategy["long-strategy"]["selection_outcome_metrics"]
    short_metrics = by_strategy["failed_breakout_reversal_short"][
        "selection_outcome_metrics"
    ]
    assert long_metrics["raw_close_change_pct"] == -1.0
    assert long_metrics["direction_adjusted_close_change_pct"] == -1.0
    assert long_metrics["path_status"] == "NEGATIVE_CLOSE"
    assert short_metrics["raw_close_change_pct"] == -1.0
    assert short_metrics["direction_adjusted_close_change_pct"] == 1.0
    assert short_metrics["mfe_pct"] == 1.0
    assert short_metrics["mae_pct"] == 0.0
    assert short_metrics["raw_path_status"] == "NEGATIVE_CLOSE"
    assert short_metrics["path_status"] == "POSITIVE_CLOSE"
    assert all(row["learning_eligible"] is True for row in rows)


def test_provider_disagreement_is_never_learning_eligible(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    outcome = _complete_selection_outcome()
    outcome["source_conflict"] = True
    outcome["independent_reconciliation_status"] = "DISAGREEMENT"
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [outcome],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    assert len(rows) == 2
    assert all(row["outcome_status"] == "INELIGIBLE" for row in rows)
    assert all(row["learning_eligible"] is False for row in rows)
    assert all("reconciliation disagrees" in row["outcome_reason"] for row in rows)


def test_bridge_batch_reports_exact_ambiguous_and_unmatched_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    first = _complete_selection_outcome()
    second = deepcopy(first)
    second["outcome_artifact_id"] = "selection-outcome:duplicate"
    unmatched = {
        **first,
        "selection_id": "selection:other",
        "signal_id": "signal:other",
    }
    stats = bridge.build_and_persist_research_episode_outcome_bridges(
        SQLiteScanStore(tmp_path / "counts.sqlite"),
        [_selection()],
        [first, second, unmatched],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    assert stats["status"] == "INELIGIBLE"
    assert stats["expected_selection_count"] == 1
    assert stats["expected_contributor_count"] == 2
    assert stats["eligible_count"] == 0
    assert stats["missing_count"] == 0
    assert stats["ineligible_count"] == 2
    assert stats["unmatched_count"] == 1
    assert stats["ambiguous_count"] == 2
    assert len(stats["bridges"]) == 2
    assert all(
        row["outcome_match_status"] == "AMBIGUOUS" for row in stats["bridges"]
    )


def test_retained_canonical_bar_payload_tamper_fails_even_after_rehash(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    row = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_complete_selection_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )[0]
    bridge.validate_research_episode_outcome_bridge(row)
    hostile = deepcopy(row)
    bars = hostile["source_bar_payload"]
    assert isinstance(bars, list)
    bars[10]["close"] = 100.25
    bars[10]["high"] = 100.25
    body = {
        key: value
        for key, value in hostile.items()
        if key not in {"bridge_id", "bridge_hash_sha256", "created_at"}
    }
    hostile["bridge_hash_sha256"] = bridge._digest(body)
    hostile["bridge_id"] = "rep-" + hostile["bridge_hash_sha256"][:24]
    with pytest.raises(SnapshotValidationError, match="bar payload hash mismatch"):
        bridge.validate_research_episode_outcome_bridge(hostile)


def test_eligible_bridge_requires_exact_authoritative_receipt_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_complete_selection_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    store = SQLiteScanStore(tmp_path / "orphan-receipt.sqlite")
    with pytest.raises(StorageError, match="exact persisted strategy decision receipt"):
        bridge.persist_research_episode_outcome_bridges(store, rows)
    assert store.load_research_episode_outcome_bridges() == []
    _insert_research_bridge_directly(store.db_path, rows[0])
    snapshot = load_strategy_learning_database_snapshot_readonly(
        store.db_path,
        market_date="2026-08-28",
        date_cutoff="2026-08-28T21:00:00+00:00",
    )
    authenticated = snapshot["research_episode_outcomes"]
    assert authenticated is not None
    assert len(authenticated) == 0
    assert authenticated.invalid_count == 1
    assert any(
        "exact persisted strategy decision receipt is absent" in reason
        for reason in authenticated.invalid_reasons
    )


def test_caller_supplied_orphan_r2_is_rejected_by_validation_and_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    row = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )[0]
    hostile = deepcopy(row)
    hostile["logical_key"] += "-r2"
    body = {
        key: value
        for key, value in hostile.items()
        if key not in {"bridge_id", "bridge_hash_sha256", "created_at"}
    }
    hostile["bridge_hash_sha256"] = bridge._digest(body)
    hostile["bridge_id"] = "rep-" + hostile["bridge_hash_sha256"][:24]
    with pytest.raises(SnapshotValidationError, match="minted by the persistence boundary"):
        bridge.validate_research_episode_outcome_bridge(hostile)
    store = SQLiteScanStore(tmp_path / "orphan-r2.sqlite")
    with pytest.raises(StorageError, match="only be minted by the store"):
        bridge.persist_research_episode_outcome_bridges(store, [hostile])
    assert store.load_research_episode_outcome_bridges() == []


def test_missing_outcome_does_not_inherit_neighbor_or_become_zero(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert len(rows) == 2
    assert {row["outcome_status"] for row in rows} == {"MISSING"}
    assert all(row["learning_eligible"] is False for row in rows)


def test_cross_date_and_unauthenticated_outcomes_are_ineligible(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    cross_date = {**_outcome(), "market_date": "2026-08-27"}
    conflicting_lineage = {**_outcome(), "selection_id": "selection:evil"}
    unauthenticated = {
        **_outcome(),
        "source_authenticated": False,
        "automatic_sourced_data": True,
    }
    for outcome in (cross_date, conflicting_lineage, unauthenticated):
        rows = bridge.build_research_episode_outcome_bridges(
            [_selection()],
            [outcome],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )
        assert {row["outcome_status"] for row in rows} == {"INELIGIBLE"}
        assert all(row["learning_eligible"] is False for row in rows)


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome_status": "STALE_OBSERVATION"},
        {"source_coverage_complete": False},
        {"source_coverage_complete": None},
        {"coverage_maximum_gap_seconds": None},
        {"coverage_maximum_gap_seconds": 61, "coverage_allowed_gap_seconds": 60},
    ],
)
def test_stale_or_gapped_source_is_ineligible(monkeypatch, mutation) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [{**_outcome(), **mutation}],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert {row["outcome_status"] for row in rows} == {"INELIGIBLE"}
    assert all(row["learning_eligible"] is False for row in rows)


def test_bridge_retry_is_idempotent_and_collision_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
        created_at="2026-08-28T21:01:00+00:00",
    )
    store = SQLiteScanStore(tmp_path / "bridge.sqlite")
    assert bridge.persist_research_episode_outcome_bridges(store, rows)["inserted"] == 2
    replay = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
        created_at="2026-08-28T23:59:00+00:00",
    )
    assert [row["bridge_id"] for row in replay] == [row["bridge_id"] for row in rows]
    assert bridge.persist_research_episode_outcome_bridges(store, replay)["reused"] == 2
    for mutation in (
        {"outcome_status": "LOSS"},
        {"source_bar_hash_sha256": "3" * 64},
        {"source_observation_hash_sha256": "4" * 64},
        {"source_lineage": [{"source": "different-provider", "request": "changed"}]},
    ):
        conflict = bridge.build_research_episode_outcome_bridges(
            [_selection()],
            [{**_outcome(), **mutation}],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )
        with pytest.raises(StorageError, match="hash mismatch|identity/payload mismatch"):
            bridge.persist_research_episode_outcome_bridges(store, [conflict[0]])


def test_logical_key_is_delimiter_collision_safe() -> None:
    left = bridge._logical_key(
        market_date="2026-08-28",
        selection_id="selection|primary",
        strategy_id="v1",
        strategy_version="receipt",
        receipt_id="r",
    )
    right = bridge._logical_key(
        market_date="2026-08-28",
        selection_id="selection",
        strategy_id="primary|v1",
        strategy_version="receipt",
        receipt_id="r",
    )
    assert left != right


def test_invalid_frozen_selection_fails_closed(monkeypatch) -> None:
    def reject(*_args, **_kwargs):
        raise ValueError("bad slate")

    monkeypatch.setattr(bridge, "validate_ranked_research_slate", reject)
    with pytest.raises(SnapshotValidationError, match="frozen slate"):
        bridge.build_research_episode_outcome_bridges(
            [_selection()],
            [_outcome()],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )


def test_frozen_timestamp_and_lineage_overrides_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    payload = dict(selection["payload_json"])
    payload["selected_at"] = "2026-08-28T14:01:00+00:00"
    selection["payload_json"] = payload
    with pytest.raises(SnapshotValidationError, match="timestamp"):
        bridge.build_research_episode_outcome_bridges(
            [selection],
            [_outcome()],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )
    selection = _selection()
    selection["episode_id"] = "episode:" + "e" * 32
    with pytest.raises(SnapshotValidationError, match="episode identity"):
        bridge.build_research_episode_outcome_bridges(
            [selection],
            [_outcome()],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )


def test_missing_capture_can_recover_as_immutable_r2_revision(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    store = SQLiteScanStore(tmp_path / "recovery.sqlite")
    selection = _selection()
    _persist_contributor_receipts(store, selection)
    store.persist_signal_selections(
        [
            {
                "selection_id": selection["selection_id"],
                "scan_id": "scan:nova",
                "signal_id": selection["signal_id"],
                "ticker": selection["ticker"],
                "rank": 1,
                "strategy_id": "research_radar",
                "strategy_version": "v1",
                "cohort": "research_radar",
                "decision": "conditional_paper_watch",
                "selected_at": selection["selected_at"],
                "event_key": "alphaops:scan:nova:alpha_morning_watch",
                "body_sha256": "a" * 64,
                "payload_json": selection["payload_json"],
            }
        ]
    )
    missing = bridge.build_research_episode_outcome_bridges(
        [selection],
        [],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    missing_stats = bridge.persist_research_episode_outcome_bridges(store, missing)
    assert missing_stats["inserted"] == 2
    complete = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_complete_selection_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    recovered = bridge.persist_research_episode_outcome_bridges(store, complete)
    assert recovered["inserted"] == 2
    assert len(recovered["persisted_rows"]) == 2
    assert all(row["logical_key"].endswith("-r2") for row in recovered["persisted_rows"])
    missing_by_receipt = {row["receipt_id"]: row for row in missing_stats["persisted_rows"]}
    recovered_by_receipt = {row["receipt_id"]: row for row in recovered["persisted_rows"]}
    assert {
        (
            mapping["from_bridge_id"],
            mapping["from_logical_key"],
            mapping["to_bridge_id"],
            mapping["to_logical_key"],
        )
        for mapping in recovered["revision_mappings"]
    } == {
        (
            missing_by_receipt[receipt_id]["bridge_id"],
            missing_by_receipt[receipt_id]["logical_key"],
            recovered_by_receipt[receipt_id]["bridge_id"],
            recovered_by_receipt[receipt_id]["logical_key"],
        )
        for receipt_id in recovered_by_receipt
    }
    replay = bridge.persist_research_episode_outcome_bridges(store, complete)
    assert replay["reused"] == 2
    assert replay["persisted_rows"] == recovered["persisted_rows"]
    assert {
        mapping["to_bridge_id"] for mapping in replay["revision_mappings"]
    } == {row["bridge_id"] for row in recovered["persisted_rows"]}
    loaded = store.load_research_episode_outcome_bridges(market_date="2026-08-28")
    assert len(loaded) == 4
    assert sum(row["outcome_status"] == "MISSING" for row in loaded) == 2
    assert sum(row["outcome_status"] == "POSITIVE_CLOSE" for row in loaded) == 2
    snapshot = load_strategy_learning_database_snapshot_readonly(
        store.db_path,
        market_date="2026-08-28",
        date_cutoff="2026-08-28T21:00:00+00:00",
    )
    effective = snapshot["research_episode_outcomes"]
    assert effective is not None
    assert len(effective) == 2
    assert effective.persisted_history_count == 4
    assert effective.expected_selection_count == 1
    assert effective.expected_contributor_count == 2
    assert effective.invalid_count == 0
    assert all(row["logical_key"].endswith("-r2") for row in effective)
    assert all(row["learning_eligible"] is True for row in effective)
    learning_summary = _bridge_learning_summary(
        effective,
        [
            contributor["decision_receipt"]
            for contributor in selection["payload_json"]["signal"][
                "strategy_contributors"
            ]
        ],
        ingress={"source_status": "PROVIDED", "invalid_count": 0},
        expected_selection_count=effective.expected_selection_count,
        expected_contributor_count=effective.expected_contributor_count,
    )
    assert learning_summary["status"] == "COMPLETE"
    assert learning_summary["observed_selection_count"] == 1
    assert learning_summary["observed_contributor_count"] == 2
    assert learning_summary["unexpected_count"] == 0


def test_readonly_database_loader_authenticates_exact_bridge_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    store = SQLiteScanStore(tmp_path / "authenticated-bridge.sqlite")
    selection = _selection()
    _persist_contributor_receipts(store, selection)
    store.persist_signal_selections(
        [
            {
                "selection_id": selection["selection_id"],
                "scan_id": "scan:nova",
                "signal_id": selection["signal_id"],
                "ticker": selection["ticker"],
                "rank": 1,
                "strategy_id": "research_radar",
                "strategy_version": "v1",
                "cohort": "research_radar",
                "decision": "conditional_paper_watch",
                "selected_at": selection["selected_at"],
                "event_key": "alphaops:scan:nova:alpha_morning_watch",
                "body_sha256": "a" * 64,
                "payload_json": selection["payload_json"],
            }
        ]
    )
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_complete_selection_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T20:00:00+00:00",
    )
    bridge.persist_research_episode_outcome_bridges(store, rows)
    snapshot = load_strategy_learning_database_snapshot_readonly(
        store.db_path,
        market_date="2026-08-28",
        date_cutoff="2026-08-28T21:00:00+00:00",
    )
    batch = snapshot["research_episode_outcomes"]
    assert batch is not None
    assert len(batch) == 2
    assert batch.expected_selection_count == 1
    assert batch.expected_contributor_count == 2
    assert batch.invalid_count == 0
    accepted, ingress = _validated_research_bridges(
        batch,
        market_date="2026-08-28",
        cutoff=datetime.fromisoformat("2026-08-28T21:00:00+00:00"),
    )
    assert len(accepted) == 2
    assert ingress == {
        "source_status": "PROVIDED",
        "invalid_count": 0,
        "invalid_reasons": {},
    }
    frozen = scanner_cli._serialize_learning_batch(
        batch,
        provenance="persisted_research_bridge",
    )
    restored = scanner_cli._restore_learning_batch(
        frozen,
        allowed_provenance="persisted_research_bridge",
        authenticated=True,
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert restored is not None
    assert len(restored) == 2
    assert restored.expected_selection_count == 1
    assert restored.expected_contributor_count == 2
    hostile = deepcopy(frozen)
    hostile["items"][0]["envelope"]["ticker"] = "OTHER"
    with pytest.raises(SnapshotValidationError, match="restore failed"):
        scanner_cli._restore_learning_batch(
            hostile,
            allowed_provenance="persisted_research_bridge",
            authenticated=True,
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )


def test_direct_mapping_bridges_are_diagnostics_only() -> None:
    receipts = (
        {
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "strategy_id": "primary",
            "strategy_version": "v1",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
            "outcome_state": "MISSING",
        },
        {
            "receipt_id": "sdr-other",
            "receipt_hash_sha256": "9" * 64,
            "strategy_id": "other",
            "strategy_version": "v1",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
        },
        {
            "receipt_id": "sdr-secondary",
            "receipt_hash_sha256": "e" * 64,
            "strategy_id": "secondary",
            "strategy_version": "v2",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
            "outcome_state": "MISSING",
        },
    )
    joined = [
        {
            "bridge_id": "rep-one",
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "strategy_id": "primary",
            "strategy_version": "v1",
            "ticker": "NOVA",
            "market_date": "2026-08-28",
                "selection_outcome": "POSITIVE_CLOSE",
            "learning_eligible": True,
        },
        {
            "bridge_id": "rep-two",
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "strategy_id": "primary",
            "strategy_version": "v1",
            "ticker": "NOVA",
            "market_date": "2026-08-28",
                "selection_outcome": "NEGATIVE_CLOSE",
            "learning_eligible": True,
        },
        {
            "bridge_id": "rep-three",
            "receipt_id": "sdr-secondary",
            "receipt_hash_sha256": "e" * 64,
            "strategy_id": "secondary",
            "strategy_version": "v2",
            "ticker": "NOVA",
            "market_date": "2026-08-28",
                "selection_outcome": "POSITIVE_CLOSE",
            "learning_eligible": True,
        },
    ]
    overlaid = _apply_research_episode_outcomes(receipts, joined)
    summary = _aggregate_decision_receipts(overlaid)
    assert summary["outcome_state_counts"]["MISSING_OUTCOME"] == 3
    accepted, ingress = _validated_research_bridges(
        joined,
        market_date="2026-08-28",
        cutoff=datetime.fromisoformat("2026-08-28T21:00:00+00:00"),
    )
    assert accepted == ()
    assert ingress["source_status"] == "INTEGRITY_FAILURE"
    assert ingress["invalid_count"] == 3
    assert ingress["invalid_reasons"] == {
        "bridge_not_from_persisted_readonly_source": 3
    }
    reversed_overlay = _apply_research_episode_outcomes(receipts, list(reversed(joined)))
    assert reversed_overlay == overlaid
