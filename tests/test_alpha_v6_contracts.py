from __future__ import annotations

from pathlib import Path

from intraday_scanner.alpha.v6.contracts import decision_contract_violations
from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, get_schema_version
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_v6_contract_rejects_non_point_in_time_or_broker_enabled_decision() -> None:
    violations = decision_contract_violations(
        {
            "decision_id": "d1",
            "market_date": "2026-08-03",
            "decision_at": "2026-08-03T12:00:00+00:00",
            "ticker": "NOVA",
            "strategy_version": "v6",
            "model_version": "v6m",
            "action": "SHADOW_TRACK",
            "input_hash_sha256": "a" * 64,
            "source_lineage_hash_sha256": "b" * 64,
            "point_in_time": {"all_inputs_observed_at_or_before_decision": False},
            "safety_vetoes": [],
            "research_only": True,
            "broker_execution_enabled": True,
        }
    )

    assert "point_in_time_lineage_invalid" in violations
    assert "broker_execution_must_be_disabled" in violations


def test_v6_research_schema_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "v6.sqlite"
    SQLiteScanStore(path).initialize()

    with __import__("sqlite3").connect(path) as connection:
        assert get_schema_version(connection) == CURRENT_SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "alpha_v6_labels",
        "alpha_v6_datasets",
        "alpha_v6_model_artifacts",
        "alpha_v6_shadow_predictions",
        "alpha_v6_drift_reports",
        "alpha_v6_promotion_reviews",
    } <= tables
