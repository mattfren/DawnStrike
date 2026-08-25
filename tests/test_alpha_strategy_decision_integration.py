import sqlite3
from typing import Any

import pytest

from intraday_scanner.config import load_config
from intraday_scanner.decisioning.condition_registry import registry_for_strategy
from intraday_scanner.services.alpha_cycle_service import (
    _apply_receipt_risk_gates,
    _apply_strategy_decision_receipts,
    _signal_payload,
)
from intraday_scanner.services.daily_strategy_learning_service import (
    _aggregate_decision_receipts,
)
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _supported_signal(strategy_id: str = "ts_momentum_sma_atr") -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": "TEST",
        "strategy_id": strategy_id,
        "strategy_version": "v1",
        "market_date": "2026-08-22",
        "alpha_score": 90,
        "breakout_trigger": 10,
        "invalidation": 9,
        "target_1": 12,
        "reward_risk_ratio": 2,
    }
    row.update({spec.condition_id: True for spec in registry_for_strategy(strategy_id)})
    return row


def test_alpha_signal_payload_binds_active_strategy_contract() -> None:
    payload = _signal_payload(
        {"ticker": "TEST", "alpha_score": 90},
        "scan-1",
        "2026-08-25T13:00:00+00:00",
        1,
    )

    assert payload["strategy_id"] == "alphaops_v5"
    assert payload["strategy_version"] == "dawnstrike-alphaops-v5.0.0"
    assert payload["signal_key"] == "scan-1:1:TEST"


class _FakeGapResolver:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resolve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        condition_ids = [str(item) for item in kwargs["condition_ids"]]
        return {
            "status": "completed",
            "condition_results": [
                {
                    "condition_id": condition_id,
                    "status": "RESOLVED_FROM_SOURCE",
                    "observed_value": "fixture source-grounded context",
                    "reason": "mocked cited evidence",
                    "source_urls": ["https://example.com/fixture"],
                    "source_hashes": ["a" * 64],
                    "resolver_id": "strategy_gap_resolver",
                    "resolution_method": "mocked_provider",
                    "requested_model": "fixture-model",
                    "actual_model": "fixture-model",
                }
                for condition_id in condition_ids
            ],
            "claims": [],
        }


def test_strategy_evidence_defaults_disabled_and_shadow_only(monkeypatch) -> None:
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY", raising=False)
    config = load_config()
    assert config.strategy_evidence_enabled is False
    assert config.strategy_evidence_shadow_only is True


def test_strategy_evidence_flags_can_be_enabled_without_broker_execution(monkeypatch) -> None:
    monkeypatch.setenv("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY", "true")
    config = load_config()
    assert config.strategy_evidence_enabled is True
    assert config.strategy_evidence_shadow_only is True


def test_receipt_integration_covers_supported_rows_after_legacy_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DAWNSTRIKE_CODE_SHA", "a" * 40)
    config = load_config(
        strategy_evidence_enabled=True,
        strategy_evidence_shadow_only=True,
        strategy_evidence_max_candidates=1,
        alert_score_threshold=0,
    )
    legacy = {"ticker": "LEGACY", "strategy_id": "alphaops_v5", "rank": 1}
    first = _supported_signal()
    first["rank"] = 2
    second = _supported_signal("donchian_breakout_20_10")
    second["ticker"] = "SECOND"
    second["rank"] = 3
    store = SQLiteScanStore(tmp_path / "receipts.sqlite")

    stats = _apply_strategy_decision_receipts(
        [legacy, first, second],
        store=store,
        config=config,
        decision_at="2026-08-22T14:30:00+00:00",
        source_summary={"run_id": "source-run-1"},
    )

    assert stats["computed"] == 2
    assert stats["resolution_candidates"] == 1
    assert stats["resolution_deferred"] == 1
    assert stats["uncovered_candidates"] == 1
    assert first["receipt_id"].startswith("sdr-")
    assert second["receipt_id"].startswith("sdr-")
    assert len(store.load_strategy_decision_receipts()) == 2


def test_receipt_resolution_orders_supported_candidates_and_respects_budget(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DAWNSTRIKE_CODE_SHA", "c" * 40)
    config = load_config(
        strategy_evidence_enabled=True,
        strategy_evidence_shadow_only=True,
        strategy_evidence_max_candidates=2,
        alert_score_threshold=0,
    )
    rows = []
    for ticker, rank in (("THIRD", 3), ("FIRST", 1), ("SECOND", 2)):
        row = _supported_signal()
        row["ticker"] = ticker
        row["rank"] = rank
        row.pop("offering_or_dilution")
        rows.append(row)
    resolver = _FakeGapResolver()

    stats = _apply_strategy_decision_receipts(
        rows,
        store=SQLiteScanStore(tmp_path / "ordered.sqlite"),
        config=config,
        decision_at="2026-08-22T14:30:00+00:00",
        source_summary={"source_identity": "fixture-source"},
        gap_resolver=resolver,
    )

    assert stats["selected_candidates"] == 2
    assert stats["resolution_deferred"] == 1
    assert stats["resolver_candidates"] == 2
    assert [call["symbol"] for call in resolver.calls] == ["FIRST", "SECOND"]
    assert rows[0]["strategy_evidence_resolution_status"] == "not_resolver_supported"
    assert rows[1]["pick_tier"] == "QUALIFIED_PICK"
    assert rows[2]["pick_tier"] == "QUALIFIED_PICK"


def test_shadow_receipt_risk_helper_preserves_legacy_cohort() -> None:
    row = {
        "ticker": "SHADOW",
        "can_alert": True,
        "no_trade_reason": "legacy disposition",
        "strategy_receipt_enabled": True,
        "strategy_receipt_shadow_only": True,
        "strategy_receipt_construction_status": "COMPLETE",
        "receipt_id": "sdr-fixture",
        "strategy_receipt_tier": "BLOCKED_DATA",
        "strategy_receipt_research_pick_eligible": False,
        "strategy_receipt_paper_entry_eligible": False,
    }

    _apply_receipt_risk_gates([row], [])

    assert row["can_alert"] is True
    assert row["no_trade_reason"] == "legacy disposition"
    assert row["broker_execution_enabled"] is not True


def test_non_shadow_receipt_risk_helper_blocks_research_alert_only() -> None:
    row = _supported_signal()
    row.update(
        {
            "premarket_price": 10,
            "premarket_volume": 1000,
            "source_confidence": 90,
            "strategy_receipt_enabled": True,
            "strategy_receipt_shadow_only": False,
            "strategy_receipt_construction_status": "COMPLETE",
            "receipt_id": "sdr-fixture",
            "strategy_receipt_tier": "BLOCKED_DATA",
            "strategy_receipt_research_pick_eligible": False,
            "strategy_receipt_paper_entry_eligible": False,
            "broker_execution_enabled": False,
        }
    )

    _apply_receipt_risk_gates([row], [])

    assert row["can_alert"] is False
    assert "strategy_receipt_ineligible" in row["hard_avoid_reasons"]
    assert row["broker_execution_enabled"] is False


def test_supported_candidate_gets_explicit_fail_closed_construction_status(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("DAWNSTRIKE_CODE_SHA", raising=False)
    config = load_config(strategy_evidence_enabled=True, strategy_evidence_shadow_only=False)
    row = _supported_signal()

    stats = _apply_strategy_decision_receipts(
        [row],
        store=SQLiteScanStore(tmp_path / "construction.sqlite"),
        config=config,
        decision_at="2026-08-22T14:30:00+00:00",
        source_summary={"source_identity": "fixture-source"},
    )

    assert stats["computed"] == 0
    assert row["strategy_receipt_construction_status"] == "FAIL_CLOSED"
    assert row["strategy_receipt_construction_record"]["status"] == "FAIL_CLOSED"
    assert row["strategy_receipt_gap"] == "missing code identity"


def test_final_receipt_explicitly_keeps_broker_execution_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DAWNSTRIKE_CODE_SHA", "d" * 40)
    row = _supported_signal()
    config = load_config(
        strategy_evidence_enabled=True,
        strategy_evidence_shadow_only=False,
        alert_score_threshold=0,
    )

    _apply_strategy_decision_receipts(
        [row],
        store=SQLiteScanStore(tmp_path / "broker.sqlite"),
        config=config,
        decision_at="2026-08-22T14:30:00+00:00",
        source_summary={"source_identity": "fixture-source"},
    )

    assert row["strategy_receipt_construction_status"] == "COMPLETE"
    assert row["research_only"] is True
    assert row["broker_execution_enabled"] is False


def test_receipt_migration_repairs_partial_sidecar_and_protects_children(tmp_path) -> None:
    database = tmp_path / "partial-receipts.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (30, '2026-08-22T00:00:00+00:00')")
        connection.execute(
            """CREATE TABLE strategy_decision_receipts (
                receipt_id TEXT PRIMARY KEY,
                receipt_hash_sha256 TEXT NOT NULL UNIQUE,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market_date TEXT NOT NULL,
                pick_tier TEXT NOT NULL,
                research_pick_eligible INTEGER NOT NULL,
                paper_entry_eligible INTEGER NOT NULL,
                source_identity TEXT NOT NULL,
                input_hash_sha256 TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.commit()
        assert run_migrations(connection) == 30
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert {
            "strategy_decision_receipts",
            "strategy_condition_results",
            "strategy_evidence_claims",
            "strategy_evidence_resolution_runs",
        } <= tables
        assert "strategy_condition_results_no_update" in triggers
        assert "strategy_evidence_claims_no_delete" in triggers


def test_receipt_retry_reuses_exact_payload_and_child_rows_are_append_only(tmp_path) -> None:
    from intraday_scanner.services.strategy_decision_service import StrategyDecisionService

    receipt = StrategyDecisionService(
        code_sha="b" * 40,
        source_identity="fixture-source",
        score_threshold=0,
    ).build_receipt(_supported_signal())
    store = SQLiteScanStore(tmp_path / "retry.sqlite")

    assert store.persist_strategy_decision_receipt(receipt) is True
    assert store.persist_strategy_decision_receipt(receipt) is False
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE strategy_condition_results SET status = 'FAIL' WHERE receipt_id = ?",
                (receipt.receipt_id,),
            )


def test_daily_learning_preserves_receipt_outcome_and_gap_dimensions() -> None:
    aggregate = _aggregate_decision_receipts(
        [
            {
                "strategy_id": "ts_momentum_sma_atr",
                "strategy_version": "v1",
                "pick_tier": "PICK_WITH_DISCLOSED_GAPS",
                "research_pick_eligible": True,
                "paper_entry_eligible": False,
                "outcome_state": "WIN",
                "all_blocking_failures": ["point_in_time_ohlcv"],
                "disclosed_gaps": ["catalyst_identified"],
                "condition_results": [
                    {
                        "condition_id": "point_in_time_ohlcv",
                        "status": "MISSING_DISCLOSED",
                    },
                    {
                        "condition_id": "catalyst_identified",
                        "status": "RESOLVED_FROM_SOURCE",
                        "resolver_id": "strategy_gap_resolver",
                    },
                ],
            },
            {
                "strategy_id": "ts_momentum_sma_atr",
                "strategy_version": "v1",
                "pick_tier": "BLOCKED_DATA",
                "research_pick_eligible": False,
                "paper_entry_eligible": False,
                "condition_results": [],
            },
        ]
    )

    assert aggregate["outcome_state_counts"]["WIN"] == 1
    assert aggregate["outcome_state_counts"]["MISSING_OUTCOME"] == 1
    assert aggregate["ai_resolvable_gaps_successfully_resolved"][0]["condition_id"] == (
        "catalyst_identified"
    )
    assert aggregate["disclosed_gap_outcomes"][0]["outcome_state"] == "WIN"
    assert aggregate["conditions_that_excluded_eventual_winners"][0]["condition_id"] == (
        "point_in_time_ohlcv"
    )
