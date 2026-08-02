from __future__ import annotations

import hashlib
import json

from intraday_scanner.alpha.v6.decision_ledger import build_candidate_decisions
from intraday_scanner.cli import main
from intraday_scanner.services.alpha_v6_universe_adapter_service import (
    RAW_ARTIFACT_SCHEMA,
    SOURCE_CONTRACT_SCHEMA,
    build_alpha_v6_universe_candidate,
)
from intraday_scanner.services.alpha_v6_universe_service import (
    active_alpha_v6_membership_by_ticker,
    preview_alpha_v6_universe,
    register_alpha_v6_universe,
    restore_alpha_v6_universe,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _lineage(source_id: str = "fixture") -> dict[str, str]:
    return {
        "source_id": source_id,
        "provider_id": "fixture_provider",
        "dataset_id": "fixture_dataset",
        "dataset_version": "v1",
        "terms_reference": "fixture://terms",
        "entitlement_reference": "fixture://entitlement",
        "accountable_contact": "operator@example.test",
        "approval_status": "APPROVED",
        "critical_truth_complete": True,
        "registration_allowed": True,
        "source_contract_hash_sha256": "c" * 64,
        "retrieved_at": "2026-08-03T12:00:00+00:00",
        "raw_artifact_sha256": "a" * 64,
        "configuration_hash_sha256": "b" * 64,
    }


def test_versioned_universe_is_immutable_and_resolves_at_decision_date(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    registered = register_alpha_v6_universe(
        store,
        as_of_date="2026-08-03",
        source_lineage=_lineage(),
        members=[
            {"ticker": "ALFA", "listing_status": "ACTIVE", "source_ref": "fixture"},
            {
                "ticker": "OLD",
                "listing_status": "DELISTED",
                "valid_from": "2026-08-01",
                "valid_to": "2026-08-02",
            },
        ],
    )

    resolved = active_alpha_v6_membership_by_ticker(
        store, market_date="2026-08-03", tickers=["ALFA", "OLD", "UNKNOWN"]
    )

    assert registered["persisted"] is True
    assert resolved["ALFA"]["status"] == "ACTIVE"
    assert resolved["ALFA"]["universe_id"] == registered["universe_id"]
    assert "OLD" not in resolved


def test_missing_versioned_universe_vetoes_shadow_tracking() -> None:
    decisions = build_candidate_decisions(
        signals=[
            {
                "scan_id": "scan-1",
                "signal_id": "signal-a",
                "ticker": "ALFA",
                "timestamp": "2026-08-03T12:00:00+00:00",
                "can_alert": True,
                "alert_gate_status": "PASS",
            }
        ],
        candidates=[{"ticker": "ALFA"}],
        feature_vectors=[
            {
                "ticker": "ALFA",
                "timestamp": "2026-08-03T11:59:00+00:00",
                "config_hash": "c" * 64,
                "feature_json": {"liquidity_execution": {"spread_pct": 0.1}},
            }
        ],
        source_summary={"status": "success"},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        decision_at="2026-08-03T12:00:00+00:00",
        scan_id="scan-1",
    )

    assert decisions[0]["action"] == "SHADOW_REJECT_VETO"
    assert "versioned_universe_membership_not_active" in decisions[0]["safety_vetoes"]


def test_cli_registers_only_reproducible_adapter_candidate(tmp_path) -> None:
    raw_artifact = tmp_path / "recorded-universe.json"
    raw_artifact.write_text(
        json.dumps(
            {
                "schema_version": RAW_ARTIFACT_SCHEMA,
                "as_of_date": "2026-08-03",
                "retrieved_at": "2026-08-03T12:00:00+00:00",
                "records": [
                    {
                        "ticker": "ALFA",
                        "listing_status": "ACTIVE",
                        "identity_status": "RESOLVED",
                        "instrument_type": "COMMON_STOCK",
                        "is_otc": False,
                        "country": "US",
                        "corporate_action_status": "CLEAR",
                        "market_cap_usd": 100000000,
                        "avg_dollar_volume_20d": 1000000,
                        "source_ref": "fixture:ALFA",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_contract = tmp_path / "source-contract.json"
    source_contract.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_CONTRACT_SCHEMA,
                "provider_id": "fixture_provider",
                "dataset_id": "fixture_dataset",
                "dataset_version": "v1",
                "terms_reference": "fixture://terms",
                "entitlement_reference": "fixture://entitlement",
                "accountable_contact": "operator@example.test",
                "approval_status": "APPROVED",
                "expected_raw_artifact_sha256": hashlib.sha256(
                    raw_artifact.read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    candidate = build_alpha_v6_universe_candidate(
        source_contract_path=source_contract,
        raw_artifact_path=raw_artifact,
    )
    source_path = tmp_path / "candidate.json"
    source_path.write_text(json.dumps(candidate), encoding="utf-8")

    preview = preview_alpha_v6_universe(
        SQLiteScanStore(tmp_path / "cli.sqlite"),
        as_of_date=str(candidate["as_of_date"]),
        source_lineage=dict(candidate["source_lineage"]),
        members=list(candidate["members"]),
    )
    code = main(
        [
            "alpha-v6-register-universe",
            "--db-path",
            str(tmp_path / "cli.sqlite"),
            "--input",
            str(source_path),
            "--source-contract",
            str(source_contract),
            "--raw-artifact",
            str(raw_artifact),
            "--confirm-preview-hash",
            str(preview["preview_hash_sha256"]),
        ]
    )

    assert code == 0


def test_universe_preview_is_nonmutating_and_restore_is_forward_only(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    first = register_alpha_v6_universe(
        store,
        as_of_date="2026-08-03",
        source_lineage=_lineage("primary"),
        members=[{"ticker": "ALFA", "listing_status": "ACTIVE"}],
    )
    preview = preview_alpha_v6_universe(
        store,
        as_of_date="2026-08-04",
        source_lineage=_lineage("secondary"),
        members=[{"ticker": "BETA", "listing_status": "ACTIVE"}],
    )

    assert preview["status"] == "REQUIRES_EXPLICIT_CONFIRMATION"
    assert preview["diff"]["added_tickers"] == ["BETA"]
    assert preview["diff"]["removed_tickers"] == ["ALFA"]
    assert len(store.load_alpha_v6_universe_versions()) == 1

    restored = restore_alpha_v6_universe(
        store,
        universe_id=first["universe_id"],
        as_of_date="2026-08-05",
        operator="operator@example.com",
        reason="verified bad upstream constituent file",
    )
    repeated = restore_alpha_v6_universe(
        store,
        universe_id=first["universe_id"],
        as_of_date="2026-08-05",
        operator="operator@example.com",
        reason="verified bad upstream constituent file",
    )
    resolved = active_alpha_v6_membership_by_ticker(
        store, market_date="2026-08-05", tickers=["ALFA"]
    )

    assert restored["restored_from_universe_id"] == first["universe_id"]
    assert resolved["ALFA"]["universe_id"] == restored["universe_id"]
    assert repeated["universe_id"] == restored["universe_id"]
    assert repeated["persisted"] is False
