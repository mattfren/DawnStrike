import csv
from pathlib import Path

import pytest

import intraday_scanner.services.trade_watcher_service as watcher_module
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.cli import main
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.scenario.contracts import (
    SCENARIO_FORWARD_COHORT,
    SCENARIO_POLICY_VERSION,
    SCENARIO_STRATEGY_ID,
)
from intraday_scanner.services.trade_watcher_service import run_trade_watcher
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_trade_watcher_enters_once_and_persists_paper_fill(tmp_path: Path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            _bar("2026-06-22T09:35:00-04:00", 10.3),
        ],
    )
    _persist_signal(SQLiteScanStore(db_path))

    first = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        dry_run=True,
    )
    second = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        dry_run=True,
    )

    store = SQLiteScanStore(db_path)
    intents = store.load_trade_intents(market_date="2026-06-22")
    positions = store.load_paper_positions(market_date="2026-06-22")
    fills = store.load_paper_trade_fills(market_date="2026-06-22")
    notifications = store.load_recent_notifications(limit=10)

    assert first["intent_stats"]["inserted"] == 1
    assert first["paper_fill_stats"]["inserted"] == 1
    assert second["intent_stats"]["inserted"] == 0
    assert second["paper_fill_stats"]["inserted"] == 0
    assert intents[0]["action"] == "ENTER_LONG"
    assert positions[0]["status"] == "OPEN"
    assert fills[0]["side"] == "BUY"
    body = notifications[0]["body"]
    assert "PAPER INTENT ONLY - ENTRY SIGNAL" in body
    assert "Research/watchlist only. No broker order was placed." in body
    assert "TRADE" + " NOW" not in body


def test_trade_watcher_exits_open_position_at_target(tmp_path: Path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            _bar("2026-06-22T09:35:00-04:00", 10.3),
            _bar("2026-06-22T09:40:00-04:00", 11.6),
        ],
    )
    _persist_signal(SQLiteScanStore(db_path))

    run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        dry_run=True,
    )
    exit_run = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:40",
        minute_bars=bars,
        dry_run=True,
    )

    store = SQLiteScanStore(db_path)
    intents = store.load_trade_intents(market_date="2026-06-22")
    positions = store.load_paper_positions(market_date="2026-06-22")
    fills = store.load_paper_trade_fills(market_date="2026-06-22")
    signal_events = store.load_signal_events(signal_id="sig-NOVA")

    assert exit_run["intent_stats"]["inserted"] == 1
    assert {row["action"] for row in intents} == {"ENTER_LONG", "EXIT_LONG"}
    assert positions[0]["status"] == "CLOSED"
    assert positions[0]["realized_return_pct"] is not None
    assert {row["side"] for row in fills} == {"BUY", "SELL"}
    assert any(row["event_type"] == "EXIT_SIGNAL" for row in signal_events)


def test_trade_watcher_live_execute_is_locked(tmp_path: Path) -> None:
    with pytest.raises(SnapshotValidationError, match="live_execute is locked"):
        run_trade_watcher(
            db_path=tmp_path / "scanner.sqlite",
            mode="live_execute",
            market_date="2026-06-22",
            requested_at="09:35",
        )


def test_v5_trade_watcher_risk_sizes_an_official_entry(tmp_path: Path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [_bar("2026-07-31T10:00:00-04:00", 10.05)],
    )
    _persist_v5_signal(SQLiteScanStore(db_path))

    result = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-07-31",
        requested_at="10:00",
        minute_bars=bars,
        dry_run=True,
        simulated_equity=100_000,
    )

    store = SQLiteScanStore(db_path)
    positions = store.load_paper_positions(market_date="2026-07-31")
    fills = store.load_paper_trade_fills(market_date="2026-07-31")
    intents = store.load_trade_intents(market_date="2026-07-31")

    assert result["paper_fill_stats"]["inserted"] == 1
    assert positions[0]["quantity"] == 216
    assert positions[0]["notional"] <= 10_000
    assert positions[0]["account_id"] == ALPHAOPS_V5_ACCOUNT_ID
    assert positions[0]["official_paper_eligible"] is True
    assert fills[0]["quantity"] == 216
    assert intents[0]["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert intents[0]["strategy_version"] == ALPHAOPS_V5_STRATEGY_VERSION
    assert intents[0]["risk_amount"] <= 250
    assert len(intents[0]["decision_fingerprint"]) == 64
    assert intents[0]["decision_trace"]["feasibility_score"] == 100


def test_v5_trade_watcher_never_fills_a_watch_only_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [_bar("2026-07-31T10:00:00-04:00", 10.05)],
    )
    _persist_v5_signal(
        SQLiteScanStore(db_path),
        decision="probability_fallback",
        alert_gate_status="WATCH_ONLY",
        manual_confirmation_required=True,
    )

    result = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-07-31",
        requested_at="10:00",
        minute_bars=bars,
        dry_run=True,
    )

    store = SQLiteScanStore(db_path)
    intents = store.load_trade_intents(market_date="2026-07-31")
    assert result["paper_fill_stats"]["inserted"] == 0
    assert store.load_paper_positions(market_date="2026-07-31") == []
    assert intents[0]["action"] == "STAND_DOWN"
    assert intents[0]["official_paper_eligible"] is False
    assert "selection_not_clean_edge" in intents[0]["decision_trace"]["reasons"]
    assert "manual_confirmation_required" in intents[0]["decision_trace"]["reasons"]


def test_trade_watcher_fails_closed_without_exact_session_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_historical_signals(
        [
            {
                "signal_id": "unselected-signal",
                "scan_id": "scan-unselected",
                "generated_at": "2026-06-22T13:20:00Z",
                "market_date": "2026-06-22",
                "ticker": "NOVA",
                "rank": 1,
                "signal_label": "WATCH",
                "entry_watch_level": 10.25,
                "invalidation_level": 9.5,
                "target_1": 11.5,
                "raw_payload_json": {},
            }
        ]
    )

    with pytest.raises(SnapshotValidationError, match="selection evidence is absent"):
        run_trade_watcher(
            db_path=db_path,
            source="csv",
            market_date="2026-06-22",
            requested_at="09:35",
            minute_bars=_write_minute_bars(
                tmp_path / "bars.csv",
                [_bar("2026-06-22T09:35:00-04:00", 10.3)],
            ),
            dry_run=True,
        )


def test_trade_watcher_executes_only_valid_scenario_paper_selection(tmp_path: Path) -> None:
    db_path = tmp_path / "scenario.sqlite"
    store = SQLiteScanStore(db_path)
    market_date = "2026-08-03"
    store.persist_historical_signals(
        [
            {
                "signal_id": "scenario:decision-1",
                "scan_id": "scenario:2026-08-03",
                "generated_at": "2026-08-03T14:00:00Z",
                "market_date": market_date,
                "ticker": "NOVA",
                "rank": 1,
                "signal_label": "scenario_enter_long",
                "entry_watch_level": 10.25,
                "invalidation_level": 9.5,
                "target_1": 11.5,
                "raw_payload_json": {
                    "research_only": True,
                    "cost_model_version": "scenario-paper-fill-slippage-v1",
                },
            }
        ]
    )
    store.persist_signal_selections(
        [
            {
                "selection_id": "scenario-selection:decision-1",
                "scan_id": "scenario:2026-08-03",
                "signal_id": "scenario:decision-1",
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": SCENARIO_STRATEGY_ID,
                "strategy_version": SCENARIO_POLICY_VERSION,
                "cohort": SCENARIO_FORWARD_COHORT,
                "decision": "paper_entry",
                "selected_at": "2026-08-03T14:00:00Z",
                "event_key": "scenario-paper:decision-1",
                "body_sha256": "scenario-body-hash",
            }
        ]
    )
    store.upsert_scenario_signal_links(
        [
            {
                "decision_id": "decision-1",
                "signal_id": "scenario:decision-1",
                "scan_id": "scenario:2026-08-03",
                "cohort": SCENARIO_FORWARD_COHORT,
                "strategy_id": SCENARIO_STRATEGY_ID,
                "strategy_version": SCENARIO_POLICY_VERSION,
                "created_at": "2026-08-03T14:00:00Z",
                "updated_at": "2026-08-03T14:00:00Z",
            }
        ]
    )

    blocked = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date=market_date,
        requested_at="10:00",
        minute_bars=_write_minute_bars(
            tmp_path / "scenario-decision-bar.csv",
            [_bar("2026-08-03T09:59:00-04:00", 10.3)],
        ),
        include_scenarios=True,
        dry_run=True,
    )

    assert blocked["paper_fill_stats"]["inserted"] == 0
    assert blocked["states"][0]["state"] == "STALE_DATA"
    assert "entry_not_after_decision_bar" in blocked["states"][0]["reason"]

    result = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date=market_date,
        requested_at="10:06",
        minute_bars=_write_minute_bars(
            tmp_path / "scenario-bars.csv",
            [_bar("2026-08-03T10:05:00-04:00", 10.3)],
        ),
        include_scenarios=True,
        dry_run=True,
    )

    positions = store.load_paper_positions(market_date=market_date)
    entry_link = store.load_scenario_signal_links(decision_id="decision-1")[0]
    assert result["signal_count"] == 1
    assert result["paper_fill_stats"]["inserted"] == 1
    assert result["scenario_link_stats"]["refreshed"] == 1
    assert positions[0]["strategy_id"] == SCENARIO_STRATEGY_ID
    assert positions[0]["cohort"] == SCENARIO_FORWARD_COHORT
    assert entry_link["paper_intent_id"] == positions[0]["entry_intent_id"]
    assert entry_link["entry_fill_id"]
    assert entry_link["exit_fill_id"] == ""
    assert entry_link["paper_trade_id"] == positions[0]["position_id"]

    exit_result = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date=market_date,
        requested_at="10:10",
        minute_bars=_write_minute_bars(
            tmp_path / "scenario-exit-bars.csv",
            [
                _bar("2026-08-03T10:05:00-04:00", 10.3),
                _bar("2026-08-03T10:10:00-04:00", 11.6),
            ],
        ),
        include_scenarios=True,
        dry_run=True,
    )
    closed = store.load_paper_positions(market_date=market_date)[0]
    exit_link = store.load_scenario_signal_links(decision_id="decision-1")[0]

    assert exit_result["paper_fill_stats"]["inserted"] == 1
    assert closed["status"] == "CLOSED"
    assert exit_link["entry_intent_id"] == closed["entry_intent_id"]
    assert exit_link["exit_intent_id"] == closed["exit_intent_id"]
    assert exit_link["entry_fill_id"]
    assert exit_link["exit_fill_id"]


def test_trade_watcher_fails_closed_on_partially_persisted_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
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
                "selected_at": "2026-06-22T13:20:00Z",
                "event_key": "alphaops:partial:alpha_morning_watch",
                "body_sha256": "partial-body-hash",
            }
        ]
    )

    with pytest.raises(SnapshotValidationError, match="partially persisted"):
        run_trade_watcher(
            db_path=db_path,
            source="csv",
            market_date="2026-06-22",
            requested_at="09:35",
            minute_bars=tmp_path / "unused.csv",
            dry_run=True,
        )


def test_notification_failure_retries_from_durable_intent_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [_bar("2026-06-22T09:35:00-04:00", 10.3)],
    )
    _persist_signal(SQLiteScanStore(db_path))
    real_dispatch = watcher_module._dispatch_notifications

    def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(watcher_module, "_dispatch_notifications", fail_dispatch)
    with pytest.raises(RuntimeError, match="telegram unavailable"):
        run_trade_watcher(
            db_path=db_path,
            source="csv",
            market_date="2026-06-22",
            requested_at="09:35",
            minute_bars=bars,
            dry_run=True,
        )
    store = SQLiteScanStore(db_path)
    assert len(store.load_trade_intents(market_date="2026-06-22")) == 1
    assert store.load_recent_notifications(limit=10) == []

    monkeypatch.setattr(watcher_module, "_dispatch_notifications", real_dispatch)
    retried = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        dry_run=True,
    )

    assert retried["intent_stats"]["inserted"] == 0
    assert retried["notification_outbox"]["candidate_count"] == 1
    assert retried["notification_stats"]["sent"] == 1
    assert len(store.load_recent_notifications(limit=10)) == 1


def test_prior_day_open_position_is_closed_from_canonical_eod_repair(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_signal(store)
    entry_bars = _write_minute_bars(
        tmp_path / "entry-bars.csv",
        [_bar("2026-06-22T09:35:00-04:00", 10.3)],
    )
    run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=entry_bars,
        dry_run=True,
    )
    store.persist_strategy_reconciliation(
        evaluations=[],
        paper_trades=[_canonical_trade()],
        learning_labels=[],
        scorecards=[],
    )
    _persist_no_trade_selection(store, "2026-06-23")

    repaired = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-23",
        requested_at="09:35",
        minute_bars=tmp_path / "unused.csv",
        dry_run=True,
    )
    position = store.load_paper_positions(signal_id="sig-NOVA")[0]

    assert repaired["prior_open_position_count"] == 1
    assert repaired["canonical_eod_repair_count"] == 1
    assert repaired["carried_open_position_count"] == 0
    assert position["status"] == "CLOSED"
    assert position["source_reconciliation_trade_id"] == "canonical-trade-nova"
    assert position["canonical_net_return_pct"] == 5.0
    fills = store.load_paper_trade_fills(signal_id="sig-NOVA")
    assert {row["side"] for row in fills} == {"BUY", "SELL"}
    repair_fill = next(row for row in fills if row["side"] == "SELL")
    assert repair_fill["canonical_eod_repair"] is True
    assert repair_fill["source_reconciliation_trade_id"] == "canonical-trade-nova"


def test_prior_day_open_position_is_safely_carried_and_revisited_without_eod_truth(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scanner.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_signal(store)
    run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=_write_minute_bars(
            tmp_path / "entry-bars.csv",
            [_bar("2026-06-22T09:35:00-04:00", 10.3)],
        ),
        dry_run=True,
    )
    _persist_no_trade_selection(store, "2026-06-23")

    carried = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-23",
        requested_at="09:35",
        minute_bars=_write_minute_bars(
            tmp_path / "carry-bars.csv",
            [_bar("2026-06-23T09:35:00-04:00", 10.4)],
        ),
        dry_run=True,
    )

    assert carried["prior_open_position_count"] == 1
    assert carried["canonical_eod_repair_count"] == 0
    assert carried["carried_open_position_count"] == 1
    assert carried["states"][0]["state"] == "PAPER_OPEN"
    assert store.load_paper_positions(signal_id="sig-NOVA")[0]["status"] == "OPEN"


def test_trade_watcher_lifecycle_batch_rolls_back_every_table(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")
    unencodable = object()

    with pytest.raises(TypeError, match="JSON serializable"):
        store.persist_trade_watcher_lifecycle(
            intents=[
                {
                    "intent_id": "intent-atomic",
                    "signal_id": "signal-atomic",
                    "market_date": "2026-06-22",
                    "ticker": "NOVA",
                    "mode": "paper_execute",
                    "lifecycle_state": "ENTRY_TRIGGERED",
                    "action": "ENTER_LONG",
                    "decision_time": "2026-06-22T13:35:00+00:00",
                    "reason": "test",
                    "created_at": "2026-06-22T13:35:00+00:00",
                }
            ],
            paper_positions=[
                {
                    "position_id": "position-atomic",
                    "signal_id": "signal-atomic",
                    "market_date": "2026-06-22",
                    "ticker": "NOVA",
                    "status": "OPEN",
                    "quantity": 10,
                    "entry_intent_id": "intent-atomic",
                    "updated_at": "2026-06-22T13:35:00+00:00",
                }
            ],
            paper_fills=[
                {
                    "fill_id": "fill-atomic",
                    "position_id": "position-atomic",
                    "intent_id": "intent-atomic",
                    "signal_id": "signal-atomic",
                    "market_date": "2026-06-22",
                    "ticker": "NOVA",
                    "side": "BUY",
                    "fill_time": "2026-06-22T13:35:00+00:00",
                    "fill_price": 10.0,
                    "quantity": 10,
                    "gross_notional": 100.0,
                    "slippage_bps": 0.0,
                }
            ],
            signal_events=[
                {
                    "event_id": "event-valid",
                    "signal_id": "signal-atomic",
                    "event_type": "ENTRY_SIGNAL",
                    "event_timestamp": "2026-06-22T13:35:00+00:00",
                },
                {
                    "event_id": "event-invalid",
                    "signal_id": "signal-atomic",
                    "event_type": "ENTRY_SIGNAL",
                    "event_timestamp": "2026-06-22T13:35:01+00:00",
                    "payload_json": {"bad": unencodable},
                },
            ],
        )

    assert store.load_trade_intents(market_date="2026-06-22") == []
    assert store.load_paper_positions(market_date="2026-06-22") == []
    assert store.load_paper_trade_fills(market_date="2026-06-22") == []
    assert store.load_signal_events(signal_id="signal-atomic") == []


def test_trade_watcher_lifecycle_batch_accepts_empty_inputs(tmp_path: Path) -> None:
    stats = SQLiteScanStore(tmp_path / "scanner.sqlite").persist_trade_watcher_lifecycle(
        intents=[],
        paper_positions=[],
        paper_fills=[],
        signal_events=[],
    )

    assert stats == {
        "intents": {"inserted": 0, "skipped": 0, "row_count": 0},
        "paper_positions": {"inserted": 0, "row_count": 0},
        "paper_fills": {"inserted": 0, "skipped": 0, "row_count": 0},
        "signal_events": {"inserted": 0, "skipped": 0},
    }


def test_trade_watcher_consumes_only_the_frozen_selected_cohort(tmp_path: Path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_signal(store)
    store.persist_historical_signals(
        [
            {
                "signal_id": "sig-BLOCKED",
                "scan_id": "scan-1",
                "generated_at": "2026-06-22T13:20:00+00:00",
                "market_date": "2026-06-22",
                "ticker": "BLOCK",
                "rank": 2,
                "signal_label": "WATCH",
                "entry_watch_level": 10.25,
                "invalidation_level": 9.5,
                "target_1": 11.5,
                "raw_payload_json": {"can_alert": True},
            }
        ]
    )
    store.persist_signal_selections(
        [
            {
                "selection_id": "selected-nova",
                "scan_id": "scan-1",
                "signal_id": "sig-NOVA",
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "clean_edge",
                "selected_at": "2026-06-22T13:20:00+00:00",
                "event_key": "alphaops:scan-1:watch",
                "body_sha256": "body-hash",
            }
        ]
    )
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            _bar("2026-06-22T09:35:00-04:00", 10.3),
            {
                **_bar("2026-06-22T09:35:00-04:00", 10.3),
                "ticker": "BLOCK",
            },
        ],
    )

    result = run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        dry_run=True,
    )

    assert result["signal_count"] == 1
    assert [row["ticker"] for row in result["paper_fills"]] == ["NOVA"]
    assert result["paper_fills"][0]["selection_id"] == "selected-nova"
    assert result["paper_fills"][0]["strategy_id"] == "alphaops_v4"


def test_trade_watcher_exit_message_is_paper_intent_only(tmp_path: Path) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(
        tmp_path / "bars.csv",
        [
            _bar("2026-06-22T09:35:00-04:00", 10.3),
            _bar("2026-06-22T09:40:00-04:00", 11.6),
        ],
    )
    _persist_signal(SQLiteScanStore(db_path))

    run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:35",
        minute_bars=bars,
        dry_run=True,
    )
    run_trade_watcher(
        db_path=db_path,
        source="csv",
        market_date="2026-06-22",
        requested_at="09:40",
        minute_bars=bars,
        dry_run=True,
    )

    notifications = SQLiteScanStore(db_path).load_recent_notifications(limit=10)
    body = "\n".join(str(row["body"]) for row in notifications)

    assert "PAPER INTENT ONLY - EXIT SIGNAL" in body
    assert "EXIT" + " NOW" not in body


def test_cli_trade_watch_runs_paper_cycle(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(tmp_path / "bars.csv", [_bar("2026-06-22T09:35:00-04:00", 10.3)])
    _persist_signal(SQLiteScanStore(db_path))

    status = main(
        [
            "trade-watch",
            "--source",
            "csv",
            "--db-path",
            str(db_path),
            "--minute-bars",
            str(bars),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:35",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    positions = SQLiteScanStore(db_path).load_paper_positions(market_date="2026-06-22")
    assert status == 0
    assert '"intent_stats"' in captured.out
    assert positions[0]["status"] == "OPEN"


def test_cli_trade_watch_loop_runs_one_iteration(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "scanner.sqlite"
    bars = _write_minute_bars(tmp_path / "bars.csv", [_bar("2026-06-22T09:35:00-04:00", 10.3)])
    _persist_signal(SQLiteScanStore(db_path))

    status = main(
        [
            "trade-watch-loop",
            "--source",
            "csv",
            "--db-path",
            str(db_path),
            "--minute-bars",
            str(bars),
            "--market-date",
            "2026-06-22",
            "--at",
            "09:35",
            "--dry-run",
            "--max-iterations",
            "1",
            "--interval-seconds",
            "0.1",
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert '"iteration": 1' in captured.out
    assert SQLiteScanStore(db_path).load_trade_intents(market_date="2026-06-22")


def _persist_signal(store: SQLiteScanStore) -> None:
    store.persist_historical_signals(
        [
            {
                "signal_id": "sig-NOVA",
                "scan_id": "scan-1",
                "generated_at": "2026-06-22T13:20:00+00:00",
                "market_date": "2026-06-22",
                "ticker": "NOVA",
                "rank": 1,
                "source": "test",
                "source_confidence": 90,
                "primary_setup": "Momentum",
                "setup_grade": "A",
                "signal_label": "WATCH",
                "entry_watch_level": 10.25,
                "invalidation_level": 9.5,
                "target_1": 11.5,
                "raw_payload_json": {},
            }
        ]
    )
    store.persist_signal_selections(
        [
            {
                "selection_id": "selected-nova",
                "scan_id": "scan-1",
                "signal_id": "sig-NOVA",
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "clean_edge",
                "selected_at": "2026-06-22T13:20:00+00:00",
                "event_key": "alphaops:scan-1:alpha_morning_watch",
                "body_sha256": "watch-body-hash",
            }
        ]
    )


def _persist_v5_signal(
    store: SQLiteScanStore,
    *,
    decision: str = "clean_edge",
    alert_gate_status: str = "PASS",
    manual_confirmation_required: bool = False,
) -> None:
    signal = {
        "signal_id": "sig-NOVA-v5",
        "scan_id": "scan-v5",
        "generated_at": "2026-07-31T12:10:00+00:00",
        "market_date": "2026-07-31",
        "ticker": "NOVA",
        "rank": 1,
        "source": "verified_snapshot",
        "source_confidence": 92,
        "source_count": 3,
        "source_quality_status": "verified",
        "stale_data_flag": False,
        "primary_setup": "Momentum",
        "setup_grade": "A",
        "signal_label": "WATCH",
        "entry_watch_level": 10.0,
        "invalidation_level": 9.0,
        "target_1": 12.75,
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
        "alert_gate_status": alert_gate_status,
        "manual_confirmation_required": manual_confirmation_required,
        "classification": (
            "WATCH ONLY" if manual_confirmation_required else "TRADE SETUP"
        ),
    }
    store.persist_historical_signals(
        [{**signal, "raw_payload_json": signal}]
    )
    store.persist_signal_selections(
        [
            {
                "selection_id": "selected-nova-v5",
                "scan_id": "scan-v5",
                "signal_id": "sig-NOVA-v5",
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
                "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
                "cohort": "official_telegram",
                "decision": decision,
                "selected_at": "2026-07-31T12:10:00+00:00",
                "event_key": "alphaops:scan-v5:alpha_morning_watch",
                "body_sha256": "watch-body-hash-v5",
                "payload_json": {
                    "decision_payload": {"decision_tier": decision},
                    "signal": signal,
                },
            }
        ]
    )


def _persist_no_trade_selection(store: SQLiteScanStore, day: str) -> None:
    store.persist_signal_selections(
        [
            {
                "selection_id": f"selected-no-trade-{day}",
                "scan_id": f"scan-no-trade-{day}",
                "signal_id": f"no_trade:{day}",
                "ticker": "NO_TRADE",
                "rank": 0,
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "no_trade",
                "selected_at": f"{day}T13:20:00+00:00",
                "event_key": f"alphaops:{day}:alpha_no_trade",
                "body_sha256": f"no-trade-body-{day}",
            }
        ]
    )


def _canonical_trade() -> dict[str, object]:
    return {
        "trade_id": "canonical-trade-nova",
        "selection_id": "selected-nova",
        "signal_id": "sig-NOVA",
        "market_date": "2026-06-22",
        "ticker": "NOVA",
        "strategy_id": "alphaops_v4",
        "strategy_version": "dawnstrike-alphaops-v4",
        "cohort": "official_telegram",
        "direction": "long",
        "decision_time": "2026-06-22T13:20:00Z",
        "entry_time": "2026-06-22T13:35:00Z",
        "entry_fill_price": 10.3,
        "exit_time": "2026-06-22T19:59:00Z",
        "exit_fill_price": 10.815,
        "exit_reason": "eod_close",
        "quantity": 97.08737864,
        "notional": 1000.0,
        "net_pnl": 50.0,
        "net_return_pct": 5.0,
        "r_multiple": 0.5,
        "fees": 0.2,
        "slippage_cost": 1.0,
        "source_bar_hash_sha256": "canonical-bars-hash",
        "execution_policy_version": "alphaops_intraday_first_touch_v1",
        "created_at": "2026-06-22T20:05:00Z",
    }


def _bar(timestamp: str, close: float) -> dict[str, str]:
    return {
        "ticker": "NOVA",
        "timestamp": timestamp,
        "open": str(close),
        "high": str(close),
        "low": str(close),
        "close": str(close),
        "volume": "1000",
    }


def _write_minute_bars(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ticker", "timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path
