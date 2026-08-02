from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.cli import main
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.services.alpha_v6_universe_adapter_service import (
    RAW_ARTIFACT_SCHEMA,
    SOURCE_CONTRACT_SCHEMA,
    build_alpha_v6_universe_candidate,
)
from intraday_scanner.services.alpha_v6_universe_service import (
    preview_alpha_v6_universe,
    register_alpha_v6_universe,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _artifact() -> dict[str, object]:
    return {
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
                "market_cap_usd": 125_000_000,
                "avg_dollar_volume_20d": 2_000_000,
                "source_ref": "record:ALFA",
            },
            {
                "ticker": "OTC1",
                "listing_status": "ACTIVE",
                "identity_status": "RESOLVED",
                "instrument_type": "COMMON_STOCK",
                "is_otc": True,
                "country": "US",
                "corporate_action_status": "CLEAR",
                "market_cap_usd": 50_000_000,
                "avg_dollar_volume_20d": 1_000_000,
                "source_ref": "record:OTC1",
            },
            {
                "ticker": "MISS",
                "listing_status": "ACTIVE",
                "identity_status": "RESOLVED",
                "instrument_type": "COMMON_STOCK",
                "is_otc": False,
                "country": "US",
                "corporate_action_status": "CLEAR",
                "market_cap_usd": 50_000_000,
                "avg_dollar_volume_20d": None,
                "source_ref": "record:MISS",
            },
        ],
    }


def _write_source_inputs(tmp_path: Path, *, approval_status: str) -> tuple[Path, Path]:
    artifact_path = tmp_path / "recorded-universe.json"
    artifact_path.write_text(json.dumps(_artifact(), sort_keys=True), encoding="utf-8")
    artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    contract_path = tmp_path / "source-contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_CONTRACT_SCHEMA,
                "provider_id": "licensed_fixture_provider",
                "dataset_id": "us-small-cap-constituents",
                "dataset_version": "2026-08-03",
                "terms_reference": "fixture://terms/accepted",
                "entitlement_reference": "fixture://entitlement/approved",
                "accountable_contact": "operator@example.test",
                "approval_status": approval_status,
                "expected_raw_artifact_sha256": artifact_hash,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return contract_path, artifact_path


def test_pending_contract_builds_preview_candidate_but_blocks_registration(tmp_path) -> None:
    contract_path, artifact_path = _write_source_inputs(
        tmp_path, approval_status="PENDING_EXTERNAL_APPROVAL"
    )
    candidate = build_alpha_v6_universe_candidate(
        source_contract_path=contract_path,
        raw_artifact_path=artifact_path,
    )

    assert candidate["status"] == "BLOCKED_EXTERNAL_APPROVAL"
    assert candidate["registration_allowed"] is False
    assert [row["ticker"] for row in candidate["members"]] == ["ALFA"]
    assert {row["ticker"] for row in candidate["rejected_members"]} == {"MISS", "OTC1"}
    store = SQLiteScanStore(tmp_path / "state.sqlite")
    preview = preview_alpha_v6_universe(
        store,
        as_of_date=str(candidate["as_of_date"]),
        members=candidate["members"],
        source_lineage=candidate["source_lineage"],
    )
    with pytest.raises(SnapshotValidationError, match="registration is blocked"):
        register_alpha_v6_universe(
            store,
            as_of_date=str(candidate["as_of_date"]),
            members=candidate["members"],
            source_lineage=candidate["source_lineage"],
        )
    assert preview["status"] == "REQUIRES_EXPLICIT_CONFIRMATION"
    assert store.load_alpha_v6_universe_versions() == []


def test_approved_contract_can_register_only_complete_members(tmp_path) -> None:
    contract_path, artifact_path = _write_source_inputs(tmp_path, approval_status="APPROVED")
    candidate = build_alpha_v6_universe_candidate(
        source_contract_path=contract_path,
        raw_artifact_path=artifact_path,
    )
    result = register_alpha_v6_universe(
        SQLiteScanStore(tmp_path / "state.sqlite"),
        as_of_date=str(candidate["as_of_date"]),
        members=candidate["members"],
        source_lineage=candidate["source_lineage"],
    )

    assert result["persisted"] is True
    assert result["members"][0]["eligibility"]["market_cap_usd"] == 125_000_000.0


def test_cli_writes_pending_candidate_and_returns_external_gate(tmp_path) -> None:
    contract_path, artifact_path = _write_source_inputs(
        tmp_path, approval_status="PENDING_EXTERNAL_APPROVAL"
    )
    output_path = tmp_path / "candidate.json"

    assert (
        main(
            [
                "alpha-v6-build-universe",
                "--source-contract",
                str(contract_path),
                "--raw-artifact",
                str(artifact_path),
                "--out",
                str(output_path),
            ]
        )
        != 0
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["registration_allowed"] is False


def test_cli_rejects_a_tampered_or_unreproducible_candidate(tmp_path) -> None:
    contract_path, artifact_path = _write_source_inputs(tmp_path, approval_status="APPROVED")
    candidate = build_alpha_v6_universe_candidate(
        source_contract_path=contract_path,
        raw_artifact_path=artifact_path,
    )
    candidate["members"].append({"ticker": "FORGED"})
    candidate_path = tmp_path / "tampered-candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    assert (
        main(
            [
                "alpha-v6-preview-universe",
                "--db-path",
                str(tmp_path / "state.sqlite"),
                "--input",
                str(candidate_path),
            ]
        )
        != 0
    )

    alternate_artifact = tmp_path / "alternate-universe.json"
    alternate_artifact.write_text(
        json.dumps(
            {
                **_artifact(),
                "records": [
                    {
                        "ticker": "BETA",
                        "listing_status": "ACTIVE",
                        "identity_status": "RESOLVED",
                        "instrument_type": "COMMON_STOCK",
                        "is_otc": False,
                        "country": "US",
                        "corporate_action_status": "CLEAR",
                        "market_cap_usd": 125_000_000,
                        "avg_dollar_volume_20d": 2_000_000,
                        "source_ref": "record:BETA",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    alternate_contract = tmp_path / "alternate-contract.json"
    alternate_contract.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_CONTRACT_SCHEMA,
                "provider_id": "licensed_fixture_provider",
                "dataset_id": "us-small-cap-constituents",
                "dataset_version": "2026-08-03",
                "terms_reference": "fixture://terms/accepted",
                "entitlement_reference": "fixture://entitlement/approved",
                "accountable_contact": "operator@example.test",
                "approval_status": "APPROVED",
                "expected_raw_artifact_sha256": hashlib.sha256(
                    alternate_artifact.read_bytes()
                ).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    alternate_candidate = build_alpha_v6_universe_candidate(
        source_contract_path=alternate_contract,
        raw_artifact_path=alternate_artifact,
    )
    candidate_path.write_text(json.dumps(alternate_candidate), encoding="utf-8")
    preview = preview_alpha_v6_universe(
        SQLiteScanStore(tmp_path / "state.sqlite"),
        as_of_date=str(alternate_candidate["as_of_date"]),
        members=list(alternate_candidate["members"]),
        source_lineage=dict(alternate_candidate["source_lineage"]),
    )
    assert (
        main(
            [
                "alpha-v6-register-universe",
                "--db-path",
                str(tmp_path / "state.sqlite"),
                "--input",
                str(candidate_path),
                "--source-contract",
                str(contract_path),
                "--raw-artifact",
                str(artifact_path),
                "--confirm-preview-hash",
                str(preview["preview_hash_sha256"]),
            ]
        )
        != 0
    )
    assert SQLiteScanStore(tmp_path / "state.sqlite").load_alpha_v6_universe_versions() == []

    candidate["candidate_hash_sha256"] = "0" * 64
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    assert (
        main(
            [
                "alpha-v6-register-universe",
                "--db-path",
                str(tmp_path / "state.sqlite"),
                "--input",
                str(candidate_path),
                "--source-contract",
                str(contract_path),
                "--raw-artifact",
                str(artifact_path),
                "--confirm-preview-hash",
                "not-reached",
            ]
        )
        != 0
    )


def test_registration_rejects_legacy_caller_supplied_lineage(tmp_path) -> None:
    with pytest.raises(SnapshotValidationError, match="provider_id"):
        register_alpha_v6_universe(
            SQLiteScanStore(tmp_path / "state.sqlite"),
            as_of_date="2026-08-03",
            members=[{"ticker": "ALFA", "listing_status": "ACTIVE"}],
            source_lineage={
                "source_id": "legacy",
                "retrieved_at": "2026-08-03T12:00:00+00:00",
                "raw_artifact_sha256": "a" * 64,
                "configuration_hash_sha256": "b" * 64,
            },
        )
