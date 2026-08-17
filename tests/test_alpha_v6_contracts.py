from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.alpha.v6.contracts import decision_contract_violations
from intraday_scanner.errors import StorageError
from intraday_scanner.services.v6_learning_service import synchronize_v6_outcomes
from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, get_schema_version
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import (
    canonical_return_outcome,
    canonical_v6_decision,
    canonical_v6_label,
    causal_identity_from,
)


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


def test_v6_label_store_rejects_same_id_with_different_payload(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = canonical_v6_decision("d-conflict")
    store.persist_alpha_v6_decisions([decision])
    label = canonical_v6_label(decision)

    assert store.persist_alpha_v6_labels([label]) == {"inserted": 1, "skipped": 0}
    assert store.persist_alpha_v6_labels([label]) == {"inserted": 0, "skipped": 1}
    with pytest.raises(StorageError, match="immutable V6 label conflict"):
        store.persist_alpha_v6_labels([{**label, "learning_eligible": False}])

    assert store.load_alpha_v6_labels() == [label]


@pytest.mark.parametrize(
    "mutation",
    ("same_value_corrected_ineligible", "different_value", "different_lineage"),
)
def test_v6_label_store_never_skips_a_correction_as_an_idempotent_duplicate(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = SQLiteScanStore(tmp_path / f"{mutation}.sqlite")
    decision = canonical_v6_decision(f"decision-{mutation}")
    label = canonical_v6_label(decision)
    store.persist_alpha_v6_decisions([decision])
    store.persist_alpha_v6_labels([label])
    corrected = dict(label)
    if mutation == "same_value_corrected_ineligible":
        corrected["learning_eligible"] = False
        corrected["return_label_eligible"] = False
    elif mutation == "different_value":
        corrected["label_value"] = float(label["label_value"]) + 1.0
    else:
        corrected["return_truth_hash_sha256"] = "f" * 64

    with pytest.raises(StorageError, match="immutable V6 label conflict"):
        store.persist_alpha_v6_labels([corrected])

    assert store.load_alpha_v6_labels() == [label]


def test_preexisting_legacy_eligible_label_cannot_mask_same_value_correction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v6.sqlite"
    store = SQLiteScanStore(db_path)
    decision = canonical_v6_decision("legacy-correction")
    current = canonical_v6_label(decision)
    legacy = {
        **current,
        "label_schema_version": "dawnstrike-alphaops-v6-label-schema-v1",
        "eligibility_policy_version": "dawnstrike.alphaops-v6-eligibility.v1",
        "return_truth_status": "LEGACY_CONTRACT",
        "learning_eligible": True,
    }
    store.persist_alpha_v6_decisions([decision])
    store.initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO alpha_v6_labels
            (label_id, decision_id, market_date, observed_at, label_family,
             label_value, learning_eligible, exclusion_reason,
             source_bar_hash_sha256, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy["label_id"],
                legacy["decision_id"],
                legacy["market_date"],
                legacy["observed_at"],
                legacy["label_family"],
                legacy["label_value"],
                1,
                None,
                legacy["source_bar_hash_sha256"],
                json.dumps(legacy, sort_keys=True),
            ),
        )
    corrected = {
        **current,
        "learning_eligible": False,
        "return_label_eligible": False,
        "return_truth_status": "LEGACY_CORRECTION_QUARANTINED",
    }

    with pytest.raises(StorageError):
        store.persist_alpha_v6_labels([corrected])

    assert store.load_alpha_v6_labels() == [legacy]


def test_v6_outcome_store_is_exact_idempotent_and_conflicting_payload_fails(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = canonical_v6_decision("outcome-conflict")
    outcome = {
        **canonical_return_outcome(
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            )
        ),
        "decision_id": decision["decision_id"],
        "shadow_signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "observed_at": "2026-08-03T21:00:00+00:00",
    }
    store.persist_alpha_v6_decisions([decision])

    assert store.persist_alpha_v6_outcomes([outcome]) == {"inserted": 1, "skipped": 0}
    assert store.persist_alpha_v6_outcomes([outcome]) == {"inserted": 0, "skipped": 1}
    with pytest.raises(StorageError, match="immutable V6 outcome conflict"):
        store.persist_alpha_v6_outcomes(
            [{**outcome, "net_excess_return_pct": 999.0}]
        )

    assert store.load_alpha_v6_outcomes() == [outcome]


def test_v6_sync_quarantines_terminal_legacy_outcome_and_reports_revision_required(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = canonical_v6_decision("revision-required")
    terminal = {
        "outcome_id": "legacy-terminal-outcome",
        "decision_id": decision["decision_id"],
        "shadow_signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "observed_at": "2026-08-03T21:00:00+00:00",
        "activation_status": "MISSING",
        "outcome_status": "TERMINAL_MISSING",
        "learning_eligible": False,
    }
    current_source = {
        **canonical_return_outcome(
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            )
        ),
        "signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "ticker": decision["ticker"],
        "captured_at": "2026-08-03T21:00:00+00:00",
        "entry_opportunity": True,
    }
    store.persist_alpha_v6_decisions([decision])
    store.persist_alpha_v6_outcomes([terminal])
    store.persist_signal_outcomes([current_source])

    result = synchronize_v6_outcomes(store)

    assert result["blocked_legacy_outcome_count"] == 1
    assert result["outcome_revision_required"] is True
    assert result["outcome_generation"] == {"inserted": 0, "skipped": 0}
    assert store.load_alpha_v6_outcomes() == [terminal]


def test_v6_sync_quarantines_legacy_complete_sourced_boolean_outcome(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = canonical_v6_decision("legacy-complete-revision")
    legacy = {
        "outcome_id": "legacy-complete-sourced-outcome",
        "decision_id": decision["decision_id"],
        "shadow_signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "observed_at": "2026-08-03T21:00:00+00:00",
        "activation_status": "ACTIVATED",
        "outcome_status": "COMPLETE_SOURCED",
        "learning_eligible": True,
        "net_excess_return_pct": 99.0,
    }
    current_source = {
        **canonical_return_outcome(
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            )
        ),
        "signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "ticker": decision["ticker"],
        "captured_at": "2026-08-03T21:00:00+00:00",
        "entry_opportunity": True,
    }
    store.persist_alpha_v6_decisions([decision])
    store.persist_alpha_v6_outcomes([legacy])
    store.persist_signal_outcomes([current_source])

    result = synchronize_v6_outcomes(store)

    assert result["blocked_legacy_outcome_count"] == 1
    assert result["outcome_revision_required"] is True
    assert result["outcome_generation"] == {"inserted": 0, "skipped": 0}
    assert store.load_alpha_v6_outcomes() == [legacy]


def test_v6_sync_blocks_conflicting_current_source_receipts_order_invariant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = canonical_v6_decision("current-source-conflict")
    causal = causal_identity_from(decision, kind="alpha_v6_shadow_decision")
    first = {
        **canonical_return_outcome(
            causal_identity=causal,
            source_artifact_identity="bars:NOVA:provider-a",
            source_artifact_hash_sha256="a" * 64,
        ),
        "signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "ticker": decision["ticker"],
    }
    second = {
        **canonical_return_outcome(
            causal_identity=causal,
            source_artifact_identity="bars:NOVA:provider-b",
            source_artifact_hash_sha256="d" * 64,
        ),
        "signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "ticker": decision["ticker"],
    }
    store.persist_alpha_v6_decisions([decision])

    for ordered in ([first, second], [second, first]):
        monkeypatch.setattr(
            store,
            "load_signal_outcomes",
            lambda *, limit=50_000, rows=ordered: list(rows),
        )
        result = synchronize_v6_outcomes(store)
        assert result["blocked_current_source_conflict_count"] == 1
        assert result["outcome_generation"] == {"inserted": 0, "skipped": 0}
        assert store.load_alpha_v6_outcomes() == []
