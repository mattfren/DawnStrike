"""Hostile, network-free checks for the Vercel publication journal contract."""

import hashlib
import json
import multiprocessing
import os
import re
import subprocess
from pathlib import Path

import pytest

from scripts import vercel_publication_journal as journal


def _account_session_report(market_date: str, source_sha: str) -> dict:
    identity = {
        "account_id": "alphaops_v5_simulated",
        "version_bucket": "v5",
        "cohort": "official_forward_paper",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
    }
    return {
        "schema_version": "dawnstrike.account_session_report.v1",
        "status": "COMPLETE",
        "market_date": market_date,
        "code_sha": source_sha,
        **identity,
        "expected_session_count": 1,
        "ledger_row_count": 1,
        "complete_count": 1,
        "missing_count": 0,
        "partial_count": 0,
        "quarantined_count": 0,
        "unsafe_ledger_count": 0,
        "input_hash_sha256": "2" * 64,
        "expected_calendar_hash_sha256": "3" * 64,
        "source_hashes_sha256": "4" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
        "series": [
            {
                "status": "COMPLETE",
                "market_date": market_date,
                "code_sha": source_sha,
                **identity,
                "expected_session_count": 1,
                "ledger_row_count": 1,
                "complete_count": 1,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        ],
    }


def _competing_stale_adopter(
    lock_path: str, state_root: str, owner: str, start: object, release: object, results: object
) -> None:
    start.wait(10)
    try:
        journal.acquire_lock(
            Path(lock_path),
            state_root=Path(state_root),
            owner_id=owner,
            candidate_source_sha="a" * 40,
            candidate_source_tree="b" * 40,
            candidate_market_date="2026-08-31",
            journal_path="outputs/journal.json",
        )
        results.put((owner, "acquired"))
        release.wait(10)
        journal.release_lock(Path(lock_path), state_root=Path(state_root), owner_id=owner)
    except Exception as exc:
        results.put((owner, type(exc).__name__))


def _pre_payload() -> dict:
    aliases = [
        "https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app",
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
    ]
    return {
        "schema_version": journal.SCHEMA,
        "operation": "vercel_publication",
        "phase": "PRE_MUTATION",
        "sequence": 0,
        "project_id": "prj_test",
        "project_name": "dawnstrike-command-center-x3",
        "provider_scope": "mattfrens-projects",
        "production_aliases": aliases,
        "candidate_preview_url": (
            "https://dawnstrike-command-center-x3-previewabc-mattfrens-projects.vercel.app"
        ),
        "candidate_preview_deployment_id": "dpl_preview",
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "toolchain_identity_sha256": "9" * 64,
        "candidate_market_date": "2026-08-31",
        "candidate_build_id": "c" * 20,
        "candidate_build_sha": "c" * 64,
        "candidate_build_manifest_sha256": "8" * 64,
        "candidate_release_manifest_sha256": "a" * 64,
        "candidate_public_artifact_root_sha256": "1" * 64,
        "candidate_manifest_sha256": "d" * 64,
        "candidate_package_manifest_sha256": "e" * 64,
        "prior_aliases": [
            {
                "alias": alias,
                "deployment_id": f"prior-{i}",
                "deployment_url": (
                    f"https://dawnstrike-command-center-x3-prior{i}-mattfrens-projects.vercel.app"
                ),
                "health_status": "alive",
                "readiness_status": "ready",
                "readiness_http_status": 200,
                "source_sha": "1" * 40,
                "source_tree": "2" * 40,
                "source_manifest_sha256": "3" * 64,
                "build_manifest_sha256": "4" * 64,
                "release_manifest_sha256": "5" * 64,
                "artifact_proof": {
                    "endpoint": (
                        "https://dawnstrike-command-center-x3-"
                        f"prior{i}-mattfrens-projects.vercel.app"
                    ),
                    "build_sha": "6" * 64,
                    "asset_count": 2,
                    "total_bytes": 100,
                    "file_hashes_sha256": "7" * 64,
                },
                "rollback_contract": {
                    "schema_version": "dawnstrike.vercel_rollback_target.v1",
                    "mode": "READY_SOURCE_MANIFEST",
                    "health_status": "alive",
                    "readiness_status": "ready",
                    "readiness_http_status": 200,
                    "readiness_reason": "complete",
                    "readiness_failed_checks": [],
                    "source_proof": {
                        "kind": "deployed_source_manifest",
                        "sha256": "3" * 64,
                    },
                },
            }
            for i, alias in enumerate(aliases)
        ],
        "promoted_deployment_id": None,
        "promoted_deployment_url": None,
        "production_result_sha256": journal.EMPTY_SHA256,
        "result_relative_path": "build/daily-deployment-result.json",
        "result_payload": None,
        "prior_journal_file_sha256": journal.EMPTY_SHA256,
        "compensation_relative_path": "NONE",
        "compensation_sha256": journal.EMPTY_SHA256,
        "recorded_at_utc": "2026-08-31T12:00:00.000000Z",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _seal(path: Path, payload: dict) -> bytes:
    raw = journal.canonical_json(payload)
    path.write_bytes(raw)
    value = dict(payload)
    value["journal_self_sha256"] = hashlib.sha256(raw).hexdigest()
    return journal.canonical_json(value)


def _pinned_legacy_payload() -> dict:
    payload = _pre_payload()
    attestation = journal.PINNED_LEGACY_ATTESTATION
    aliases = sorted(journal.PINNED_LEGACY_ALIASES)
    payload["production_aliases"] = aliases
    payload["prior_aliases"] = [
        {
            "alias": alias,
            "deployment_id": attestation["deployment_id"],
            "deployment_url": attestation["deployment_url"],
            "health_status": "alive",
            "readiness_status": "not_ready",
            "readiness_http_status": 503,
            "source_sha": attestation["source_sha"],
            "source_tree": attestation["source_tree"],
            "source_manifest_sha256": journal.EMPTY_SHA256,
            "build_manifest_sha256": attestation["build_manifest_sha256"],
            "release_manifest_sha256": attestation["release_manifest_sha256"],
            "artifact_proof": {
                "endpoint": attestation["deployment_url"],
                "build_sha": attestation["build_sha"],
                "asset_count": attestation["asset_count"],
                "total_bytes": attestation["total_bytes"],
                "file_hashes_sha256": attestation["file_hashes_sha256"],
            },
            "rollback_contract": {
                "schema_version": "dawnstrike.vercel_rollback_target.v1",
                "mode": "PINNED_LEGACY_CLOCK_STALE",
                "health_status": "alive",
                "readiness_status": "not_ready",
                "readiness_http_status": 503,
                "readiness_reason": attestation["readiness_reason"],
                "readiness_failed_checks": list(attestation["readiness_failed_checks"]),
                "source_proof": {
                    "kind": "pinned_legacy_attestation",
                    "sha256": journal.PINNED_LEGACY_ATTESTATION_SHA256,
                },
            },
        }
        for alias in aliases
    ]
    return payload


def _compensation_payload(prior: dict, *, current: bool) -> dict:
    evidence = []
    for item in prior["prior_aliases"]:
        row = {
            "alias": item["alias"],
            "expected_deployment_id": item["deployment_id"],
            "expected_deployment_url": item["deployment_url"],
            "observed_deployment_id": item["deployment_id"],
            "observed_deployment_url": item["deployment_url"],
            "restored": True,
            "health_status": item["health_status"],
            "readiness_status": item["readiness_status"],
            "readiness_http_status": item["readiness_http_status"],
            "source_sha": item["source_sha"],
            "source_tree": item["source_tree"],
            "source_manifest_sha256": item["source_manifest_sha256"],
            "build_manifest_sha256": item["build_manifest_sha256"],
            "release_manifest_sha256": item["release_manifest_sha256"],
            "artifact_proof": item["artifact_proof"],
        }
        if current:
            row["rollback_contract"] = item["rollback_contract"]
        evidence.append(row)
    compensation = {
        "schema_version": (
            journal.COMPENSATION_SCHEMA if current else journal.LEGACY_COMPENSATION_SCHEMA
        ),
        "status": "COMPENSATED",
        "operation": "vercel_publication",
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "promoted_deployment_id": None,
        "promoted_deployment_url": None,
        "prior_aliases": prior["prior_aliases"],
        "rollback_evidence": evidence,
        "rollback_status": "ROLLED_BACK",
        "failure_type": "hostile_test",
        "research_only": True,
        "broker_execution_enabled": False,
        "recorded_at_utc": "2026-08-31T12:00:00.000000Z",
    }
    compensation["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json(compensation)
    ).hexdigest()
    return compensation


def _history_kwargs(root: Path, payload: dict) -> dict:
    return {
        "history_root": root / "outputs" / "daily_finalize" / "vercel-publication",
        "state_root": root,
        "project_id": payload["project_id"],
        "project_name": payload["project_name"],
        "provider_scope": payload["provider_scope"],
        "production_aliases": payload["production_aliases"],
        "require_no_lock": True,
    }


def _write_current_compensated_journal(root: Path, path: Path) -> dict:
    prior = _pre_payload()
    prior.update(_authorization_fields())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_seal(path, prior))
    prior_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    compensation = _compensation_payload(prior, current=True)
    compensation_path = path.parent / "vercel-publication-compensation-test.json"
    compensation_path.write_bytes(journal.canonical_json(compensation))
    compensation_hash = hashlib.sha256(compensation_path.read_bytes()).hexdigest()
    compensation_relative = compensation_path.relative_to(root).as_posix()
    tombstone = {
        "schema_version": "dawnstrike.daily_deployment_compensated.v1",
        "status": "COMPENSATED",
        "market_date": prior["candidate_market_date"],
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "compensation_sha256": compensation_hash,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result_path = root / prior["result_relative_path"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(journal.canonical_json(tombstone))
    terminal = dict(prior)
    terminal.update(
        {
            "schema_version": journal.COMPENSATED_SCHEMA,
            "phase": "COMPENSATED",
            "sequence": 3,
            "compensation_relative_path": compensation_relative,
            "compensation_sha256": compensation_hash,
            "result_payload": tombstone,
            "production_result_sha256": hashlib.sha256(
                journal.canonical_json(tombstone)
            ).hexdigest(),
            "prior_journal_file_sha256": prior_hash,
        }
    )
    source = path.parent / ".compensated-input.json"
    source.write_bytes(journal.canonical_json(terminal))
    journal.transition(source, path, path, state_root=root)
    source.unlink()
    return terminal


def _write_compensated_archive_intent(*, root: Path, archive: Path, payload: dict) -> Path:
    raw_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    intent_path = archive.with_name(
        f"vercel-publication-operation-compensated-{raw_hash}.intent.json"
    )
    intent = {
        "schema_version": journal.ARCHIVE_INTENT_SCHEMA,
        "status": "ARCHIVE_REQUIRED",
        "journal_sha256": raw_hash,
        "candidate_market_date": payload["candidate_market_date"],
        "project_id": payload["project_id"],
        "project_name": payload["project_name"],
        "provider_scope": payload["provider_scope"],
        "archive_relative_path": archive.relative_to(root).as_posix(),
        "compensation_relative_path": payload["compensation_relative_path"],
        "compensation_sha256": payload["compensation_sha256"],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    intent["intent_self_sha256"] = hashlib.sha256(journal.canonical_json(intent)).hexdigest()
    intent_path.write_bytes(journal.canonical_json(intent))
    return intent_path


def test_durable_history_verifier_accepts_terminal_complete_without_runtime_result(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    payload = _complete_payload()
    history = root / "outputs" / "daily_finalize" / "vercel-publication"
    dated = history / payload["candidate_market_date"]
    dated.mkdir(parents=True)
    result_path = root / payload["result_relative_path"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(journal.canonical_json(payload["result_payload"]))
    operation = dated / journal.CANONICAL_JOURNAL_NAME
    operation.write_bytes(_seal(operation, payload))
    (history / f"{journal.PUBLICATION_LOCK_NAME}.gate").write_bytes(b"\0")

    verified = journal.verify_history(**_history_kwargs(root, payload))
    assert verified == {
        "schema_version": "dawnstrike.vercel_publication_history_verification.v1",
        "status": "PASS",
        "journal_count": 1,
        "complete_count": 1,
        "compensated_count": 0,
        "archive_count": 0,
        "intent_count": 0,
        "history_contract_sha256": verified["history_contract_sha256"],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", verified["history_contract_sha256"])


def test_durable_history_verifier_accepts_exact_compensated_archive_intent_pair(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    payload = _pre_payload()
    dated = (
        root
        / "outputs"
        / "daily_finalize"
        / "vercel-publication"
        / payload["candidate_market_date"]
    )
    operation = dated / journal.CANONICAL_JOURNAL_NAME
    terminal = _write_current_compensated_journal(root, operation)
    raw_hash = hashlib.sha256(operation.read_bytes()).hexdigest()
    archive = dated / f"vercel-publication-operation-compensated-{raw_hash}.json"
    operation.replace(archive)
    intent = _write_compensated_archive_intent(root=root, archive=archive, payload=terminal)

    verified = journal.verify_history(**_history_kwargs(root, payload))
    assert verified["journal_count"] == 0
    assert verified["archive_count"] == verified["intent_count"] == 1

    intent.unlink()
    with pytest.raises(ValueError, match="archive and intent pairs are incomplete"):
        journal.verify_history(**_history_kwargs(root, payload))


def test_durable_history_verifier_blocks_nonterminal_foreign_legacy_and_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    payload = _pre_payload()
    history = root / "outputs" / "daily_finalize" / "vercel-publication"
    dated = history / payload["candidate_market_date"]
    dated.mkdir(parents=True)
    operation = dated / journal.CANONICAL_JOURNAL_NAME
    operation.write_bytes(_seal(operation, payload))
    kwargs = _history_kwargs(root, payload)

    with pytest.raises(ValueError, match="nonterminal"):
        journal.verify_history(**kwargs)

    foreign = dict(payload)
    foreign["project_id"] = "prj_foreign"
    operation.write_bytes(_seal(operation, foreign))
    with pytest.raises(ValueError, match="foreign provider boundary"):
        journal.verify_history(**kwargs)

    operation.unlink()
    legacy = history / journal.CANONICAL_JOURNAL_NAME
    legacy.write_bytes(_seal(legacy, payload))
    with pytest.raises(ValueError, match="canonical dated layout"):
        journal.verify_history(**kwargs)

    legacy.unlink()
    (history / journal.PUBLICATION_LOCK_NAME).write_text("malformed", encoding="utf-8")
    with pytest.raises(ValueError, match="publication lock exists"):
        journal.verify_history(**kwargs)


@pytest.mark.parametrize("invalid_date", ["9999-99-99", "2026-02-29"])
def test_journal_history_lock_and_archive_intent_reject_impossible_calendar_dates(
    tmp_path: Path, invalid_date: str
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    payload = _pre_payload()
    payload["candidate_market_date"] = invalid_date
    path = root / "journal.json"
    raw = _seal(path, payload)
    with pytest.raises(ValueError, match="real calendar date"):
        journal.validate(raw)

    with pytest.raises(ValueError, match="real calendar date"):
        journal.acquire_lock(
            root / journal.PUBLICATION_LOCK_NAME,
            state_root=root,
            owner_id="calendar-test",
            candidate_source_sha="a" * 40,
            candidate_source_tree="b" * 40,
            candidate_market_date=invalid_date,
            journal_path="outputs/journal.json",
        )

    intent = {
        "schema_version": journal.ARCHIVE_INTENT_SCHEMA,
        "status": "ARCHIVE_REQUIRED",
        "journal_sha256": "1" * 64,
        "candidate_market_date": invalid_date,
        "project_id": "prj_test",
        "project_name": "dawnstrike-command-center-x3",
        "provider_scope": "mattfrens-projects",
        "archive_relative_path": "outputs/archive.json",
        "compensation_relative_path": "outputs/compensation.json",
        "compensation_sha256": "2" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    intent["intent_self_sha256"] = hashlib.sha256(journal.canonical_json(intent)).hexdigest()
    with pytest.raises(ValueError, match="real calendar date"):
        journal.validate_archive_intent(journal.canonical_json(intent))


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize("invalid_date", ["9999-99-99", "2026-02-29"])
def test_publisher_rejects_impossible_expected_market_date_before_source_or_provider(
    invalid_date: str,
) -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    function = (
        "function Resolve-VercelCanonicalMarketDate"
        + publisher.split("function Resolve-VercelCanonicalMarketDate", 1)[1].split(
            "$canonicalExpectedMarketDate =", 1
        )[0]
    )
    call = publisher.index("$canonicalExpectedMarketDate =")
    source_admission = publisher.index("$bootstrapSource = Assert-VercelRecoveryBootstrapSource")
    assert call < source_admission
    command = (
        function
        + f";try{{Resolve-VercelCanonicalMarketDate '{invalid_date}'}}catch{{"
        + "$_.Exception.Message}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert completed.stdout.strip() == ("ExpectedMarketDate is not a real canonical calendar date.")


def test_v1_active_journal_remains_read_compatible(tmp_path: Path) -> None:
    payload = _pre_payload()
    payload["schema_version"] = journal.LEGACY_SCHEMA
    for item in payload["prior_aliases"]:
        item.pop("rollback_contract")
        item["artifact_proof"]["endpoint"] = item["alias"]
    path = tmp_path / "legacy-v1.json"
    path.write_bytes(_seal(path, payload))
    assert journal.validate(path.read_bytes())["schema_version"] == journal.LEGACY_SCHEMA


def test_legacy_v2_terminal_and_v1_compensation_remain_recoverable(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    prior = _pre_payload()
    prior["schema_version"] = journal.LEGACY_SCHEMA
    prior.update(_authorization_fields())
    for item in prior["prior_aliases"]:
        item.pop("rollback_contract")
        item["artifact_proof"]["endpoint"] = item["alias"]
    journal_path = root / "journal.json"
    journal_path.write_bytes(_seal(journal_path, prior))
    prior_hash = hashlib.sha256(journal_path.read_bytes()).hexdigest()

    compensation = _compensation_payload(prior, current=False)
    compensation_path = root / "outputs" / "legacy-compensation.json"
    compensation_path.parent.mkdir()
    compensation_path.write_bytes(journal.canonical_json(compensation))
    compensation_hash = hashlib.sha256(compensation_path.read_bytes()).hexdigest()
    assert (
        journal.validate_compensation(compensation_path.read_bytes())["schema_version"]
        == journal.LEGACY_COMPENSATION_SCHEMA
    )

    tombstone = {
        "schema_version": "dawnstrike.daily_deployment_compensated.v1",
        "status": "COMPENSATED",
        "market_date": prior["candidate_market_date"],
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "compensation_sha256": compensation_hash,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result_path = root / prior["result_relative_path"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(journal.canonical_json(tombstone))
    terminal = dict(prior)
    terminal.update(
        {
            "schema_version": journal.LEGACY_COMPENSATED_SCHEMA,
            "phase": "COMPENSATED",
            "sequence": 3,
            "compensation_relative_path": "outputs/legacy-compensation.json",
            "compensation_sha256": compensation_hash,
            "result_payload": tombstone,
            "production_result_sha256": hashlib.sha256(
                journal.canonical_json(tombstone)
            ).hexdigest(),
            "prior_journal_file_sha256": prior_hash,
        }
    )
    source = root / "terminal-input.json"
    source.write_bytes(journal.canonical_json(terminal))
    journal.transition(source, journal_path, journal_path, state_root=root)
    assert (
        journal.validate(journal_path.read_bytes(), state_root=root, journal_path=journal_path)[
            "schema_version"
        ]
        == journal.LEGACY_COMPENSATED_SCHEMA
    )


def test_current_compensation_rejects_duplicate_alias_rows() -> None:
    prior = _pre_payload()
    compensation = _compensation_payload(prior, current=True)
    compensation["prior_aliases"].append(json.loads(json.dumps(compensation["prior_aliases"][-1])))
    compensation["rollback_evidence"].append(
        json.loads(json.dumps(compensation["rollback_evidence"][-1]))
    )
    unsigned = {key: value for key, value in compensation.items() if key != "receipt_self_sha256"}
    compensation["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json(unsigned)
    ).hexdigest()
    with pytest.raises(ValueError, match="sorted and unique"):
        journal.validate_compensation(journal.canonical_json(compensation))


def test_current_journal_rejects_legacy_compensation_schema_family(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    prior = _pre_payload()
    prior.update(_authorization_fields())
    journal_path = root / "journal.json"
    journal_path.write_bytes(_seal(journal_path, prior))

    legacy_prior = json.loads(json.dumps(prior))
    legacy_prior["schema_version"] = journal.LEGACY_SCHEMA
    for item in legacy_prior["prior_aliases"]:
        item.pop("rollback_contract")
        item["artifact_proof"]["endpoint"] = item["alias"]
    compensation = _compensation_payload(legacy_prior, current=False)
    compensation_path = root / "outputs" / "legacy-compensation.json"
    compensation_path.parent.mkdir()
    compensation_path.write_bytes(journal.canonical_json(compensation))
    compensation_hash = hashlib.sha256(compensation_path.read_bytes()).hexdigest()
    tombstone = {
        "schema_version": "dawnstrike.daily_deployment_compensated.v1",
        "status": "COMPENSATED",
        "market_date": prior["candidate_market_date"],
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "compensation_sha256": compensation_hash,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result_path = root / prior["result_relative_path"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(journal.canonical_json(tombstone))
    terminal = dict(prior)
    terminal.update(
        {
            "schema_version": journal.COMPENSATED_SCHEMA,
            "phase": "COMPENSATED",
            "sequence": 3,
            "compensation_relative_path": "outputs/legacy-compensation.json",
            "compensation_sha256": compensation_hash,
            "result_payload": tombstone,
            "production_result_sha256": hashlib.sha256(
                journal.canonical_json(tombstone)
            ).hexdigest(),
            "prior_journal_file_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
        }
    )
    source = root / "terminal-input.json"
    source.write_bytes(journal.canonical_json(terminal))
    with pytest.raises(ValueError, match="schema family mismatch"):
        journal.transition(source, journal_path, journal_path, state_root=root)


def test_exact_pinned_legacy_rollback_target_is_accepted(tmp_path: Path) -> None:
    payload = _pinned_legacy_payload()
    path = tmp_path / "pinned-legacy.json"
    path.write_bytes(_seal(path, payload))
    validated = journal.validate(path.read_bytes())
    assert {item["rollback_contract"]["mode"] for item in validated["prior_aliases"]} == {
        "PINNED_LEGACY_CLOCK_STALE"
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "deployment",
        "source",
        "tree",
        "manifest",
        "asset_map",
        "failed_checks",
        "attestation",
        "artifact_endpoint",
        "total_bytes",
    ],
)
def test_pinned_legacy_rollback_target_rejects_hostile_drift(tmp_path: Path, mutation: str) -> None:
    payload = _pinned_legacy_payload()
    item = payload["prior_aliases"][0]
    if mutation == "deployment":
        item["deployment_id"] = "dpl_foreign"
    elif mutation == "source":
        item["source_sha"] = "f" * 40
    elif mutation == "tree":
        item["source_tree"] = "f" * 40
    elif mutation == "manifest":
        item["source_manifest_sha256"] = "f" * 64
    elif mutation == "asset_map":
        item["artifact_proof"]["file_hashes_sha256"] = "f" * 64
    elif mutation == "failed_checks":
        item["rollback_contract"]["readiness_failed_checks"].reverse()
    elif mutation == "attestation":
        item["rollback_contract"]["source_proof"]["sha256"] = "f" * 64
    elif mutation == "total_bytes":
        item["artifact_proof"]["total_bytes"] += 1
    else:
        item["artifact_proof"]["endpoint"] = item["alias"]
    path = tmp_path / f"hostile-{mutation}.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError):
        journal.validate(path.read_bytes())


def test_pinned_legacy_rollback_target_requires_all_governed_aliases(
    tmp_path: Path,
) -> None:
    payload = _pinned_legacy_payload()
    payload["production_aliases"].pop()
    payload["prior_aliases"].pop()
    path = tmp_path / "pinned-legacy-subset.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError, match="governed set"):
        journal.validate(path.read_bytes())


@pytest.mark.parametrize("suffix", ["/", "/path", "?query=1"])
def test_current_journal_rejects_noncanonical_deployment_urls(tmp_path: Path, suffix: str) -> None:
    payload = _pre_payload()
    payload["prior_aliases"][0]["deployment_url"] += suffix
    payload["prior_aliases"][0]["artifact_proof"]["endpoint"] += suffix
    path = tmp_path / "noncanonical-url.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError, match="canonical HTTPS"):
        journal.validate(path.read_bytes())


@pytest.mark.parametrize(
    "hostile_url",
    [
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
        "https://example.com",
        "https://dawnstrike-command-center-x3-foreign-other-scope.vercel.app",
        "https://dawnstrike-command-center-x3-foreign-mattfrens-projects.vercel.app/path",
        "https://dawnstrike-command-center-x3-foreign-mattfrens-projects.vercel.app?x=1",
        "https://dawnstrike-command-center-x3-foreign-mattfrens-projects.vercel.app:443",
        "https://Dawnstrike-command-center-x3-foreign-mattfrens-projects.vercel.app",
    ],
)
def test_current_journal_rejects_mutable_or_foreign_prior_deployment_origins(
    tmp_path: Path, hostile_url: str
) -> None:
    payload = _pre_payload()
    payload["prior_aliases"][0]["deployment_url"] = hostile_url
    payload["prior_aliases"][0]["artifact_proof"]["endpoint"] = hostile_url
    path = tmp_path / "hostile-prior-origin.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError):
        journal.validate(path.read_bytes())


@pytest.mark.parametrize(
    "hostile_url",
    [
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
        "https://example.com",
        "https://dawnstrike-command-center-x3-preview-other-scope.vercel.app",
        "https://dawnstrike-command-center-x3-preview-mattfrens-projects.vercel.app/path",
        "https://dawnstrike-command-center-x3-preview-mattfrens-projects.vercel.app?x=1",
        "https://dawnstrike-command-center-x3-preview-mattfrens-projects.vercel.app:443",
        "https://Dawnstrike-command-center-x3-preview-mattfrens-projects.vercel.app",
    ],
)
def test_current_journal_rejects_mutable_or_noncanonical_candidate_origins(
    tmp_path: Path, hostile_url: str
) -> None:
    payload = _pre_payload()
    payload["candidate_preview_url"] = hostile_url
    path = tmp_path / "hostile-candidate-origin.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError):
        journal.validate(path.read_bytes())


@pytest.mark.parametrize(
    ("field", "hostile"),
    [
        ("promoted_deployment_id", ""),
        (
            "promoted_deployment_url",
            "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
        ),
        ("promoted_deployment_url", "https://example.com"),
        (
            "promoted_deployment_url",
            "https://dawnstrike-command-center-x3-promoted-other-scope.vercel.app",
        ),
        (
            "promoted_deployment_url",
            "https://dawnstrike-command-center-x3-promoted-mattfrens-projects.vercel.app/path",
        ),
        (
            "promoted_deployment_url",
            "https://dawnstrike-command-center-x3-promoted-mattfrens-projects.vercel.app?x=1",
        ),
        (
            "promoted_deployment_url",
            "https://dawnstrike-command-center-x3-promoted-mattfrens-projects.vercel.app:443",
        ),
    ],
)
def test_current_journal_rejects_invalid_promoted_clone_identity(
    tmp_path: Path, field: str, hostile: str
) -> None:
    payload = _complete_payload()
    payload[field] = hostile
    path = tmp_path / "hostile-promoted-identity.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError):
        journal.validate(path.read_bytes())


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("container_kind", "current", "expected_schema"),
    [
        ("ordered", True, journal.SCHEMA),
        ("json", True, journal.SCHEMA),
        ("json", False, journal.LEGACY_SCHEMA),
    ],
)
def test_powershell_journal_schema_detection_handles_dictionary_and_json_objects(
    tmp_path: Path, container_kind: str, current: bool, expected_schema: str
) -> None:
    publisher = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    property_helpers = (
        "function Test-VercelObjectProperty"
        + publisher.split("function Test-VercelObjectProperty", 1)[1].split(
            "function Set-VercelAlias", 1
        )[0]
    )
    deployment_helpers = (
        "function Normalize-VercelDeploymentUrl"
        + publisher.split("function Normalize-VercelDeploymentUrl", 1)[1].split(
            "function Assert-VercelPriorAliasSnapshotsCurrent", 1
        )[0]
    )
    payload_function = (
        "function New-VercelPublicationJournalPayload"
        + publisher.split("function New-VercelPublicationJournalPayload", 1)[1].split(
            "function Get-VercelJournalCandidateDeployment", 1
        )[0]
    )

    source_payload = _pre_payload()
    aliases = source_payload["production_aliases"]
    prior_aliases = json.loads(json.dumps(source_payload["prior_aliases"]))
    if not current:
        for item in prior_aliases:
            item.pop("rollback_contract")
            item["artifact_proof"]["endpoint"] = item["alias"]

    def ps_quote(value: str) -> str:
        return value.replace("'", "''")

    prior_json = ps_quote(json.dumps(prior_aliases, separators=(",", ":")))
    aliases_json = ps_quote(json.dumps(aliases, separators=(",", ":")))
    ordered_conversion = ""
    if container_kind == "ordered":
        ordered_conversion = (
            "$prior=@($records|ForEach-Object{$map=[ordered]@{};"
            "foreach($property in $_.PSObject.Properties){"
            "$map[$property.Name]=$property.Value};Write-Output -NoEnumerate $map});"
        )
    else:
        ordered_conversion = "$prior=@($records);"
    command = (
        "$ErrorActionPreference='Stop';"
        + property_helpers
        + deployment_helpers
        + "function Get-VercelResultSha256 {param($Value);'0' * 64};"
        + payload_function
        + "$emptySha256='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';"  # pragma: allowlist secret  # noqa: E501
        + "$expectedSourceTree='b' * 40;$resultRelativePath='build/result.json';"
        + "$ProjectId='prj_test';$ProjectName='dawnstrike-command-center-x3';"
        + "$ProviderScope='mattfrens-projects';$toolchainIdentitySha256='9' * 64;"
        + f"$allProductionAliases=@((ConvertFrom-Json '{aliases_json}')|ForEach-Object{{$_}});"
        + f"$records=@((ConvertFrom-Json '{prior_json}')|ForEach-Object{{$_}});"
        + ordered_conversion
        + "$candidate=[pscustomobject]@{id='dpl_preview';url='"
        + source_payload["candidate_preview_url"]
        + "'};"
        + "$manifest=[pscustomobject]@{source_sha=('a' * 40);market_date='2026-08-31';"
        + "build_id=('c' * 20);build_sha=('c' * 64)};"
        + "$payload=New-VercelPublicationJournalPayload -Phase PRE_MUTATION -Sequence 0 "
        + "-CandidateDeployment $candidate -PreviewManifest $manifest "
        + "-PackageManifestSha256 ('e' * 64) -CandidateBuildManifestSha256 ('8' * 64) "
        + "-CandidateReleaseManifestSha256 ('a' * 64) "
        + "-CandidatePublicArtifactRootSha256 ('1' * 64) "
        + "-CandidateManifestSha256 ('d' * 64) -PriorAliases $prior "
        + "-PromotedDeployment $null -ResultPayload $null;"
        + "$payload|ConvertTo-Json -Depth 40 -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == expected_schema
    path = tmp_path / f"{container_kind}-{expected_schema}.json"
    path.write_bytes(_seal(path, payload))
    assert journal.validate(path.read_bytes())["schema_version"] == expected_schema


def test_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    path = root / "journal.json"
    raw = b'{"a":1,"a":2}'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="duplicate"):
        journal.validate(raw)


def test_exact_keys_self_hash_and_tamper_are_enforced(tmp_path: Path) -> None:
    payload = _pre_payload()
    path = tmp_path / "pre.json"
    path.write_bytes(_seal(path, payload))
    validated = journal.validate(path.read_bytes())
    assert validated["phase"] == "PRE_MUTATION"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["candidate_source_sha"] = "f" * 40
    with pytest.raises(ValueError, match="self hash"):
        journal.validate(journal.canonical_json(tampered))
    tampered.pop("operation")
    tampered["journal_self_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="keys are not exact"):
        journal.validate(journal.canonical_json(tampered))


def test_transitions_are_adjacent_and_atomic(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    pre = root / "journal.json"
    authorization = "f" * 64
    pre_payload = _pre_payload()
    pre_payload.update(
        {
            "expected_market_date": "2026-08-31",
            "prepublication_authorization_id": authorization,
            "daily_ledger_authorization_id": authorization,
        }
    )
    pre.write_bytes(_seal(pre, pre_payload))
    prior_hash = hashlib.sha256(pre.read_bytes()).hexdigest()
    result = {
        "status": "PRODUCTION_VERIFIED",
        "source_sha": "a" * 40,
        "expected_market_date": "2026-08-31",
        "prepublication_authorization_id": authorization,
        "daily_ledger_authorization_id": authorization,
        "account_session_report": _account_session_report("2026-08-31", "a" * 40),
    }
    result["account_session_report_sha256"] = hashlib.sha256(
        journal.canonical_json(result["account_session_report"])
    ).hexdigest()
    post = dict(pre_payload)
    post.update(
        {
            "phase": "POST_ALIASES",
            "sequence": 1,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": (
                "https://dawnstrike-command-center-x3-promotedabc-mattfrens-projects.vercel.app"
            ),
            "production_result_sha256": hashlib.sha256(journal.canonical_json(result)).hexdigest(),
            "result_payload": result,
            "prior_journal_file_sha256": prior_hash,
        }
    )
    source = root / "post-input.json"
    source.write_bytes(journal.canonical_json(post))
    sealed = journal.transition(source, pre, pre)
    assert sealed["payload"]["phase"] == "POST_ALIASES"
    assert journal.validate(pre.read_bytes())["sequence"] == 1
    bad = dict(post)
    bad["phase"] = "COMPLETE"
    bad["sequence"] = 9
    bad["prior_journal_file_sha256"] = hashlib.sha256(pre.read_bytes()).hexdigest()
    bad_source = root / "bad-input.json"
    bad_source.write_bytes(journal.canonical_json(bad))
    with pytest.raises(ValueError, match="adjacent"):
        journal.transition(bad_source, pre, pre)


def test_post_aliases_requires_current_publication_authorization(tmp_path: Path) -> None:
    payload = _pre_payload()
    result = {"status": "PRODUCTION_VERIFIED", "source_sha": "a" * 40}
    payload.update(
        {
            "phase": "POST_ALIASES",
            "sequence": 1,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": (
                "https://dawnstrike-command-center-x3-promotedabc-mattfrens-projects.vercel.app"
            ),
            "production_result_sha256": hashlib.sha256(journal.canonical_json(result)).hexdigest(),
            "result_payload": result,
        }
    )
    path = tmp_path / "post.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError, match="prepublication_authorization_id"):
        journal.validate(path.read_bytes())


def _authorization_fields() -> dict[str, str]:
    authorization = "f" * 64
    return {
        "expected_market_date": "2026-08-31",
        "prepublication_authorization_id": authorization,
        "daily_ledger_authorization_id": authorization,
    }


def _complete_payload() -> dict:
    payload = _pre_payload()
    payload.update(_authorization_fields())
    payload.update(
        {
            "phase": "COMPLETE",
            "sequence": 2,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": (
                "https://dawnstrike-command-center-x3-promotedabc-mattfrens-projects.vercel.app"
            ),
        }
    )
    account_session_report = _account_session_report(
        payload["candidate_market_date"], payload["candidate_source_sha"]
    )
    payload["result_payload"] = {
        "schema_version": "dawnstrike.daily_deployment.v1",
        "preview_url": payload["candidate_preview_url"],
        "preview_deployment_id": payload["candidate_preview_deployment_id"],
        "source_sha": payload["candidate_source_sha"],
        "source_tree": payload["candidate_source_tree"],
        "market_date": payload["candidate_market_date"],
        "build_id": payload["candidate_build_id"],
        "build_sha": payload["candidate_build_sha"],
        "project_id": payload["project_id"],
        "provider_scope": payload["provider_scope"],
        "promoted_deployment_id": payload["promoted_deployment_id"],
        "production_deployment_id": payload["promoted_deployment_id"],
        "vercel_source_manifest_sha256": payload["candidate_manifest_sha256"],
        "vercel_package_manifest_sha256": payload["candidate_package_manifest_sha256"],
        "authorized_build_manifest_sha256": payload["candidate_build_manifest_sha256"],
        "authorized_release_manifest_sha256": payload["candidate_release_manifest_sha256"],
        "public_artifact_root_sha256": payload["candidate_public_artifact_root_sha256"],
        "account_session_report": account_session_report,
        "account_session_report_sha256": hashlib.sha256(
            journal.canonical_json(account_session_report)
        ).hexdigest(),
        "toolchain_identity_sha256": payload["toolchain_identity_sha256"],
        "build_manifest_sha256": "8" * 64,
        "allow_degraded": False,
        "promoted": True,
        "live_trading_enabled": False,
        "broker_execution_enabled": False,
        "research_only": True,
        "status": "PRODUCTION_VERIFIED",
        "preview_artifact_proof": {
            "endpoint": payload["candidate_preview_url"],
            "build_sha": payload["candidate_build_sha"],
            "asset_count": 2,
            "total_bytes": 100,
            "file_hashes_sha256": "1" * 64,
        },
        "production_artifact_proofs": [
            {
                "endpoint": alias,
                "build_sha": payload["candidate_build_sha"],
                "asset_count": 2,
                "total_bytes": 100,
                "file_hashes_sha256": "1" * 64,
            }
            for alias in payload["production_aliases"]
        ],
        **_authorization_fields(),
    }
    payload["production_result_sha256"] = hashlib.sha256(
        journal.canonical_json(payload["result_payload"])
    ).hexdigest()
    return payload


def test_reordered_terminal_json_is_rejected_even_with_valid_self_hash(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    result_path = root / "build" / "daily-deployment-result.json"
    result_path.parent.mkdir()
    payload = _complete_payload()
    result_path.write_bytes(journal.canonical_json(payload["result_payload"]))
    payload["journal_self_sha256"] = hashlib.sha256(journal.canonical_json(payload)).hexdigest()
    reordered = json.dumps(
        dict(reversed(payload.items())), ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    with pytest.raises(ValueError, match="canonical JSON"):
        journal.validate(reordered, state_root=root, journal_path=root / "journal.json")


def test_complete_terminal_and_result_are_bound_byte_for_byte(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    result_path = root / "build" / "daily-deployment-result.json"
    result_path.parent.mkdir()
    payload = _complete_payload()
    result_raw = journal.canonical_json(payload["result_payload"])
    result_path.write_bytes(result_raw)
    payload["journal_self_sha256"] = hashlib.sha256(journal.canonical_json(payload)).hexdigest()
    journal_path = root / "journal.json"
    journal_path.write_bytes(journal.canonical_json(payload))
    assert journal.validate(journal_path.read_bytes(), state_root=root, journal_path=journal_path)

    tampered_result = dict(payload["result_payload"])
    tampered_result["build_id"] = "tampered"
    result_path.write_bytes(journal.canonical_json(tampered_result))
    with pytest.raises(ValueError, match="production result raw hash"):
        journal.validate(journal_path.read_bytes(), state_root=root, journal_path=journal_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "incomplete"),
        ("extra", "incomplete"),
        ("mismatch", "proof tuples diverge"),
        ("count_mismatch", "proof tuples diverge"),
        ("oversize", "byte count"),
    ],
)
def test_complete_rejects_hostile_governed_artifact_proofs(
    tmp_path: Path, mutation: str, message: str
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    payload = _complete_payload()
    proofs = payload["result_payload"]["production_artifact_proofs"]
    if mutation == "missing":
        proofs.pop()
    elif mutation == "extra":
        proofs.append(dict(proofs[-1], endpoint="https://extra.example"))
    elif mutation == "mismatch":
        proofs[-1]["file_hashes_sha256"] = "2" * 64
    elif mutation == "count_mismatch":
        proofs[-1]["asset_count"] = 3
    else:
        proofs[-1]["total_bytes"] = 134_217_729
    payload["production_result_sha256"] = hashlib.sha256(
        journal.canonical_json(payload["result_payload"])
    ).hexdigest()
    result_path = root / payload["result_relative_path"]
    result_path.parent.mkdir(parents=True)
    result_path.write_bytes(journal.canonical_json(payload["result_payload"]))
    payload["journal_self_sha256"] = hashlib.sha256(journal.canonical_json(payload)).hexdigest()
    with pytest.raises(ValueError, match=message):
        journal.validate(
            journal.canonical_json(payload), state_root=root, journal_path=root / "journal.json"
        )


def test_publication_lock_rejects_live_owner_and_adopts_dead_owner(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    lock = root / "outputs" / "publication.lock"
    kwargs = {
        "state_root": root,
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "candidate_market_date": "2026-08-31",
        "journal_path": "outputs/journal.json",
    }
    journal.acquire_lock(lock, owner_id="owner-a", pid=os.getpid(), **kwargs)
    with pytest.raises(ValueError, match="live owner"):
        journal.acquire_lock(lock, owner_id="owner-b", pid=os.getpid(), **kwargs)
    journal.release_lock(lock, state_root=root, owner_id="owner-a", pid=os.getpid())

    journal.acquire_lock(lock, owner_id="dead-owner", pid=2_000_000, **kwargs)
    adopted = journal.acquire_lock(lock, owner_id="owner-c", pid=os.getpid(), **kwargs)
    assert adopted["owner_id"] == "owner-c"
    journal.release_lock(lock, state_root=root, owner_id="owner-c", pid=os.getpid())


def test_publication_lock_release_never_unlinks_a_replacement_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    lock = root / "outputs" / "publication.lock"
    replacement_source = root / "outputs" / "replacement.lock"
    kwargs = {
        "state_root": root,
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "candidate_market_date": "2026-08-31",
        "journal_path": "outputs/journal.json",
    }
    journal.acquire_lock(lock, owner_id="owner-a", pid=os.getpid(), **kwargs)
    journal.acquire_lock(replacement_source, owner_id="owner-b", pid=os.getpid(), **kwargs)
    replacement_raw = replacement_source.read_bytes()
    journal.release_lock(replacement_source, state_root=root, owner_id="owner-b", pid=os.getpid())

    real_replace = journal.os.replace

    def replace_then_reacquire(source: str | os.PathLike, target: str | os.PathLike) -> None:
        real_replace(source, target)
        Path(source).write_bytes(replacement_raw)

    with monkeypatch.context() as patch:
        patch.setattr(journal.os, "replace", replace_then_reacquire)
        journal.release_lock(lock, state_root=root, owner_id="owner-a", pid=os.getpid())

    remaining, _ = journal._read_lock(lock, root)
    assert remaining["owner_id"] == "owner-b"
    journal.release_lock(lock, state_root=root, owner_id="owner-b", pid=os.getpid())


def test_publication_lock_os_gate_allows_only_one_of_two_stale_adopters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    lock = root / "publication.lock"
    journal.acquire_lock(
        lock,
        state_root=root,
        owner_id="stale",
        pid=999_999_991,
        candidate_source_sha="a" * 40,
        candidate_source_tree="b" * 40,
        candidate_market_date="2026-08-31",
        journal_path="outputs/journal.json",
    )
    context = multiprocessing.get_context("spawn")
    start, release, results = context.Event(), context.Event(), context.Queue()
    workers = [
        context.Process(
            target=_competing_stale_adopter,
            args=(str(lock), str(root), owner, start, release, results),
        )
        for owner in ("adopter-a", "adopter-b")
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=15)[1], results.get(timeout=15)[1]]
    release.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert outcomes.count("acquired") == 1
    assert outcomes.count("ValueError") == 1


def test_containment_rejects_an_internal_reparse_component(tmp_path: Path) -> None:
    root = tmp_path / "state"
    target = root / "real"
    root.mkdir()
    target.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")
    with pytest.raises(ValueError, match="reparse component"):
        journal._contained(link / "journal.json", root)


def test_publisher_binds_every_recovery_phase_to_the_current_invocation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("$existingJournal = Get-VercelPublicationJournal", 1)[1].split(
        "function Get-VercelGovernedAssetProof", 1
    )[0]
    assert function.index("Existing Vercel recovery journal") < function.index("$recoveryAliases")
    for binding in (
        "candidate_market_date",
        "project_id",
        "project_name",
        "provider_scope",
        "production_aliases",
    ):
        assert binding in function


def test_publisher_crash_and_failure_seams_are_environment_guarded() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    guard = '$env:DAWNSTRIKE_TEST_VERCEL_PUBLICATION -ne "1"'
    assert guard in script
    assert script.index(guard) < script.index("function Test-VercelPromotionSeam")
    assert "Vercel publication failure and crash injection are test-only." in script


def test_publisher_uses_dated_state_history_and_byte_exact_runtime_result_copy() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    assert "$journalRoot = Join-Path $journalHistoryRoot $journalMarketKey" in script
    assert (
        "outputs/daily_finalize/$resultNamespace/$journalMarketKey/daily-deployment-result.json"
    ) in script
    assert "Assert-VercelPriorJournalHistoryTerminal" in script
    assert "prior_date_interrupted_rollover" in script
    assert "Prior-date terminal compensation verification" in script
    prior_recovery = script.split("foreach ($entry in $priorNonterminal)", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    assert "$savedResultPath = $script:resultPath" in prior_recovery
    assert "$script:resultPath = Join-Path $resolvedStateRoot" in prior_recovery
    assert "$script:resultPath = $savedResultPath" in prior_recovery
    writer = script.split("function Write-VercelResultAtomic", 1)[1].split(
        "function Assert-LowerHex64", 1
    )[0]
    assert "WriteAllBytes($temporary, $bytes)" in writer
    assert "WriteAllBytes($runtimeTemporary, $bytes)" in writer
    assert "Durable StateRoot and runtime Vercel result copies diverge" in writer


def test_scheduled_finalizer_runs_recovery_only_before_current_day_gates() -> None:
    publisher = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    finalizer = (Path(__file__).parents[1] / "scripts" / "run_daily_finalize.ps1").read_text(
        encoding="utf-8"
    )
    assert "[switch]$RecoveryOnly" in publisher
    assert "Recovery-only Vercel convergence reached a fresh publication path" in publisher
    assert "Archive-VercelCompensatedCurrentJournal" in publisher
    recovery = finalizer.index("-RecoveryOnly")
    release = finalizer.index("Resolve-DawnstrikeReleaseSha")
    boundary = finalizer.index("Resolve-DawnstrikeFinalizeMarketBoundary `", recovery)
    database = finalizer.index("if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf))")
    assert release < recovery < boundary < database


def test_recovery_bootstraps_exact_clean_origin_before_loading_helpers() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    bootstrap = script.index("$bootstrapSource = Assert-VercelRecoveryBootstrapSource")
    helper = script.index('. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")')
    assert bootstrap < helper
    assert "Vercel publisher must execute from the exact ProjectRoot being admitted." in script
    assert script.index("$executingRoot =") < bootstrap
    function = script.split("function Assert-VercelRecoveryBootstrapSource", 1)[1].split(
        "$resolvedRoot =", 1
    )[0]
    for required in (
        "refs/remotes/origin/main",
        "--untracked-files=all",
        "--ignored",
        "Get-AuthenticodeSignature",
        "core.fsmonitor=false",
        "core.hooksPath=NUL",
        "remote.origin.url",
        "$headAfter -cne $headBefore",
    ):
        assert required in function
    provider = script.split("function Invoke-VercelProcess", 1)[1].split(
        "function Assert-RemoteVercelSourceManifest", 1
    )[0]
    assert provider.index("Assert-VercelPublicationSourceStable") < provider.index(
        "Invoke-DawnstrikeJobProcess"
    )


def test_terminal_complete_is_revalidated_before_failure_compensation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    catch = script.split("catch {\n    $publicationError = $_.Exception.Message", 1)[1]
    reread = catch.index("$caughtJournal = Get-VercelPublicationJournal")
    complete = catch.index("Resolve-VercelCompletePublicationJournal")
    rollback = catch.index('Arguments @("rollback", [string]$priorPrimary.id, "--yes")')
    assert reread < complete < rollback
    assert "no provider compensation was attempted" in catch[:rollback]


def test_prior_terminal_history_allows_governed_toolchain_upgrade() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    equality = function.index("A nonterminal prior-date Vercel journal requires")
    nonterminal_loop = function.index("foreach ($entry in $priorNonterminal)")
    assert equality > nonterminal_loop
    before_classification = function[: function.index("$history +=")]
    assert "toolchain_identity_sha256 -cne $toolchainIdentitySha256" not in before_classification


def test_prior_alias_snapshot_uses_immutable_deployment_and_cas() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    snapshot = script.split("if ($Promote) {", 1)[1].split("try {\n    if ($Promote)", 1)[0]
    assert "$snapshotBaseUrl = Get-VercelImmutableDeploymentBaseUrl" in snapshot
    assert "-AliasUrl $snapshotBaseUrl" in snapshot
    assert '"$snapshotBaseUrl/build-manifest.json' in snapshot
    assert '"$snapshotBaseUrl/release-manifest.json' in snapshot
    assert "Get-VercelGovernedAssetProof -BaseUrl $snapshotBaseUrl" in snapshot
    assert snapshot.count("Assert-VercelPriorAliasSnapshotsCurrent") >= 1
    promotion = script.split("try {\n    if ($Promote)", 1)[1]
    assert promotion.index("Assert-VercelPriorAliasSnapshotsCurrent") < promotion.index(
        "$promoted = $true"
    )


def test_publisher_legacy_rollback_exception_is_exact_and_non_general() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    validator = script.split("function Assert-VercelPinnedLegacyRollbackTarget", 1)[1].split(
        "function Assert-VercelAuthorizedManifestBytes", 1
    )[0]
    for marker in (
        "dpl_H7UQb8hWkwxLVbNwSM1BAQq1t9g8",  # pragma: allowlist secret
        "5190ab6beb1b81556bfc70640c43a4cff48bd1f8",  # pragma: allowlist secret
        "5ad147c8813de9f841655f27c30b3aa59ccac1d6",  # pragma: allowlist secret
        "38165e0f80c5044b8cf18b664098dd90ee3f4bac83ee4ab685ab47e6e545bdbe",  # pragma: allowlist secret  # noqa: E501
        "335bcff3e359ced371356eeed4a9373253968650d77c6d0ecb6da723fbf53f9c",  # pragma: allowlist secret  # noqa: E501
        "$pinnedLegacyArtifactTotalBytes",
        "source_manifest_http_status -ne 404",
        "PINNED_LEGACY_CLOCK_STALE",
    ):
        assert marker in script
    assert "exact authorized pinned legacy rollback target" in validator
    assert "-PinnedLegacyInventory:$isPinnedLegacy" in script
    assert "$legacyPriorAliasCount -notin @(0, $allProductionAliases.Count)" in script
    assert "dawnstrike.vercel_publication_journal.v3" in script
    assert "dawnstrike.vercel_publication_journal.v4" in script
    assert "dawnstrike.vercel_publication_compensation.v2" in script


def test_powershell_and_python_bind_the_same_pinned_legacy_attestation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    fields = {
        "deployment_id": "pinnedLegacyDeploymentId",
        "deployment_url": "pinnedLegacyDeploymentUrl",
        "source_sha": "pinnedLegacySourceSha",
        "source_tree": "pinnedLegacySourceTree",
        "market_date": "pinnedLegacyMarketDate",
        "build_id": "pinnedLegacyBuildId",
        "build_sha": "pinnedLegacyBuildSha",
        "build_manifest_sha256": "pinnedLegacyBuildManifestSha256",
        "release_manifest_sha256": "pinnedLegacyReleaseManifestSha256",
        "file_hashes_sha256": "pinnedLegacyArtifactMapSha256",
        "readiness_reason": "pinnedLegacyReadinessReason",
    }
    for field, variable in fields.items():
        match = re.search(rf"\${variable}\s*=\s*'([^']+)'", script)
        assert match is not None
        assert match.group(1) == str(journal.PINNED_LEGACY_ATTESTATION[field])
    total_bytes = re.search(r"\$pinnedLegacyArtifactTotalBytes\s*=\s*(\d+)", script)
    attestation = re.search(r"\$pinnedLegacyAttestationSha256\s*=\s*'([0-9a-f]{64})'", script)
    assert total_bytes is not None
    assert attestation is not None
    assert int(total_bytes.group(1)) == journal.PINNED_LEGACY_ATTESTATION["total_bytes"]
    assert attestation.group(1) == journal.PINNED_LEGACY_ATTESTATION_SHA256
    assert "[int]$ArtifactProof.asset_count -ne 18" in script
    assert "[int]$EndpointProof.source_manifest_http_status -ne 404" in script
    assert (
        "$pinnedLegacyFailedChecks = @('calendar_freshness_stale_by_clock', 'market_date_stale')"
    ) in script


def test_current_rollback_proof_uses_recorded_immutable_deployment() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    restored = script.split("function Assert-VercelAliasRestored", 1)[1].split(
        "function Get-VercelRemoteHttpStatus", 1
    )[0]
    assert (
        "$proofBaseUrl = if ($hasRollbackContract) { $expectedUrl } else { $AliasUrl }" in restored
    )
    assert "-BaseUrl $proofBaseUrl -BuildManifest $restoredBuild" in restored
    assert "$evidence['rollback_contract'] = $observedRollbackContract" in restored


def test_compensation_never_overwrites_foreign_alias_state() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    compensation = script.split("function Invoke-VercelPublicationCompensation", 1)[1].split(
        "function Get-VercelJournalPreviewEvidence", 1
    )[0]
    assert compensation.index("Get-VercelCompensationPlan") < compensation.index("Set-VercelAlias")
    assert compensation.index("foreign_count -gt 0") < compensation.index("Set-VercelAlias")
    assert "Alias changed to a foreign deployment before compensation." in compensation
    catch = script.split("catch {\n    $publicationError = $_.Exception.Message", 1)[1]
    assert catch.index("Get-VercelCompensationPlan") < catch.index(
        'Arguments @("rollback", [string]$priorPrimary.id, "--yes")'
    )
    assert "no provider rollback was attempted" in catch


def test_every_terminal_success_rechecks_alias_and_promotion_identity() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    complete = script.split("function Complete-VercelJournalRecovery", 1)[1].split(
        "function Resolve-VercelCompletePublicationJournal", 1
    )[0]
    assert (
        complete.index("New-VercelRecoveredResultPayload")
        < complete.index("Test-VercelAliasSetMatches")
        < complete.index('$complete.phase = "COMPLETE"')
    )
    normal = script.split("$preResultJournal = Get-VercelPublicationJournal", 1)[1]
    assert normal.index("Test-VercelPromotedCandidateSetMatchesJournal") < normal.index(
        "$result = [ordered]@{"
    )
    assert normal.index("$preCompleteJournal = Get-VercelPublicationJournal") < normal.index(
        '-Phase "COMPLETE"'
    )


def test_recovery_retry_reauthorizes_current_session_before_mutation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    recovery_guard = script.index(
        "if ($RecoveryOnly) {\n"
        '    throw "Recovery-only Vercel convergence reached a fresh publication path."'
    )
    authorization = script.index("Assert-GovernedPublicationAuthorization", recovery_guard)
    fresh_build_guard = script.index("if (-not $recoveryRetry)", recovery_guard)
    assert recovery_guard < authorization < fresh_build_guard
    promotion = script.index("$promoted = $true")
    assert script.rfind("Assert-GovernedPublicationAuthorization", 0, promotion) > fresh_build_guard


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
def test_result_atomic_publish_executes_under_windows_powershell_51(tmp_path: Path) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Publish-VercelFileAtomic", 1)[1].split(
        "function Write-VercelResultAtomic", 1
    )[0]
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"first")
    destination.write_bytes(b"old")
    source2 = tmp_path / "source2.bin"
    destination2 = tmp_path / "destination2.bin"
    source2.write_bytes(b"second")

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Publish-VercelFileAtomic" + function + ";"
        f"Publish-VercelFileAtomic -TemporaryPath '{quote(source)}' "
        f"-DestinationPath '{quote(destination)}';"
        f"Publish-VercelFileAtomic -TemporaryPath '{quote(source2)}' "
        f"-DestinationPath '{quote(destination2)}'"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert destination.read_bytes() == b"first"
    assert destination2.read_bytes() == b"second"


def test_publisher_governed_asset_proof_is_bounded_and_covers_every_alias() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    proof = script.split("function Get-VercelGovernedAssetProof", 1)[1].split(
        "function Assert-PublicationState", 1
    )[0]
    for marker in (
        "$properties.Count -gt 256",
        "$length -gt 16777216",
        "$totalBytes -gt 134217728",
        "$relative.Contains('\\')",
        "$_ -in @('', '.', '..')",
        "governed asset hash mismatch",
        '"--max-filesize", "16777216"',
    ):
        assert marker in proof
    assert "preview_artifact_proof = $previewArtifactProof" in script
    assert "production_artifact_proofs = @($productionArtifactProofs)" in script
    alias_loop = script.split("$productionArtifactProofs += Get-VercelGovernedAssetProof", 1)[0]
    assert "foreach ($alias in $allProductionAliases)" in alias_loop
    assert "$vercel + $vercelAuth + $Arguments" in script
    assert "$vercel + $Arguments + $vercelAuth" not in script
    assert script.index("$vercel + $vercelAuth + $Arguments") < script.index(
        '"--", "--silent", "--show-error"'
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
def test_vercel_token_precedes_native_curl_separator_under_powershell_51() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Invoke-VercelProcess", 1)[1].split(
        "function Assert-RemoteVercelSourceManifest", 1
    )[0]
    command = (
        "$vercelEntryPath='vc.js';$vercel=@('--scope','scope');"
        "$vercelAuth=@('--token','sentinel');"
        "$nodePath='C:\\Program Files\\nodejs\\node.exe';"
        "$expectedCurlPath='C:\\Windows\\System32\\curl.exe';"
        "$gitPath='C:\\Program Files\\Git\\cmd\\git.exe';"
        "$uvPath='C:\\Users\\MattFields\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\uv.exe';"
        "$approvedPython=[pscustomobject]@{path='C:\\Users\\MattFields\\AppData\\Local\\Programs\\Python\\Python313\\python.exe'};"
        "function Assert-VercelPublicationToolchainStable {};"
        "function Assert-DawnstrikeSharedLockNoReparse { param($Path,$Label) };"
        "function Invoke-DawnstrikeJobProcess { param($FilePath,$ArgumentList,$WorkingDirectory,"
        "$Label,$TimeoutSeconds,$OutputDrainTimeoutSeconds,$EnvironmentOverrides);"
        "$script:captured=@($ArgumentList);[pscustomobject]@{ExitCode=0;Stdout='';Stderr=''} };"
        "function Invoke-VercelProcess" + function + ";"
        "$null=Invoke-VercelProcess -Arguments @('curl','/asset','--','--output','x') "
        "-Label test -TimeoutSeconds 1;$captured -join [char]31"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    argv = completed.stdout.strip().split(chr(31))
    assert argv.index("--token") < argv.index("curl") < argv.index("--")
    assert argv[argv.index("--token") + 1] == "sentinel"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
def test_state_relative_path_executes_under_windows_powershell_51(tmp_path: Path) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Get-VercelStateRelativePath", 1)[1].split(
        "function ConvertTo-VercelCanonicalObject", 1
    )[0]
    root = tmp_path / "state"
    target = root / "outputs" / "2026-09-01" / "journal.json"
    target.parent.mkdir(parents=True)

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Get-VercelStateRelativePath" + function + ";"
        f"Get-VercelStateRelativePath -StateRootPath '{quote(root)}' "
        f"-TargetPath '{quote(target)}'"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "outputs/2026-09-01/journal.json"
    assert "[System.IO.Path]::GetRelativePath" not in script


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
def test_publication_write_guard_rejects_windows_reparse_component(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows directory reparse creation is unavailable")
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelContainedNonReparsePath", 1)[1].split(
        "function ConvertTo-VercelCanonicalObject", 1
    )[0]

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Assert-VercelContainedNonReparsePath" + function + ";"
        f"Assert-VercelContainedNonReparsePath -RootPath '{quote(root)}' "
        f"-TargetPath '{quote(linked / 'escape.json')}'"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode != 0
    assert "reparse component" in completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize("foreign", ["project", "aliases"])
def test_prior_date_foreign_identity_never_calls_alias_provider(
    tmp_path: Path, foreign: str
) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    history = tmp_path / "history"
    dated = history / "2026-08-31"
    dated.mkdir(parents=True)
    (dated / "vercel-publication-operation.json").write_text("{}", encoding="utf-8")
    payload = _pre_payload()
    payload.update(_authorization_fields())
    if foreign == "project":
        payload["project_id"] = "prj_foreign"
    else:
        payload["production_aliases"] = [*payload["production_aliases"], "https://evil.example"]
    payload_json = json.dumps(payload, separators=(",", ":")).replace("'", "''")
    history_text = str(history).replace("'", "''")
    state_text = str(tmp_path).replace("'", "''")
    command = (
        f"$journalHistoryRoot='{history_text}';$resolvedStateRoot='{state_text}';"
        "$resolvedExpectedMarketDate='2026-09-01';$ProjectId='prj_test';"
        "$ProjectName='dawnstrike-command-center-x3';$ProviderScope='mattfrens-projects';"
        "$allProductionAliases=@('https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app',"
        "'https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app');"
        f"$script:hostilePayload=ConvertFrom-Json '{payload_json}';$script:setCalls=0;"
        "function Invoke-VercelJournalTool {[pscustomobject]@{payload=$script:hostilePayload}};"
        "function Set-VercelAlias {$script:setCalls++};"
        "function Invoke-VercelPublicationCompensation {Set-VercelAlias};"
        "function Assert-VercelPriorJournalHistoryTerminal" + function + ";"
        "try {Assert-VercelPriorJournalHistoryTerminal}catch{};$setCalls"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"
    assert "$script:allProductionAliases = @($verified.payload.production_aliases)" not in script


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize("scenario", ["multiple_prior", "future"])
def test_ambiguous_or_future_nonterminal_history_never_calls_alias_provider(
    tmp_path: Path, scenario: str
) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    history = tmp_path / "history"
    dates = ["2026-08-30", "2026-08-31"] if scenario == "multiple_prior" else ["2026-09-02"]
    for date in dates:
        dated = history / date
        dated.mkdir(parents=True)
        (dated / "vercel-publication-operation.json").write_text("{}", encoding="utf-8")
    payload = _pre_payload()
    payload.update(_authorization_fields())
    payload_json = json.dumps(payload, separators=(",", ":")).replace("'", "''")
    history_text = str(history).replace("'", "''")
    state_text = str(tmp_path).replace("'", "''")
    command = (
        f"$journalHistoryRoot='{history_text}';"
        f"$resolvedStateRoot='{state_text}';"
        "$resolvedExpectedMarketDate='2026-09-01';$ProjectId='prj_test';"
        "$ProjectName='dawnstrike-command-center-x3';$ProviderScope='mattfrens-projects';"
        "$allProductionAliases=@('https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app',"
        "'https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app');"
        f"$script:basePayload=ConvertFrom-Json '{payload_json}';$script:setCalls=0;"
        "function Invoke-VercelJournalTool {"
        "$copy=$script:basePayload|ConvertTo-Json -Depth 20|ConvertFrom-Json;"
        "$copy.candidate_market_date=Split-Path (Split-Path $Arguments[1] -Parent) -Leaf;"
        "[pscustomobject]@{payload=$copy}};function Set-VercelAlias {$script:setCalls++};"
        "function Invoke-VercelPublicationCompensation {Set-VercelAlias};"
        "function Assert-VercelPriorJournalHistoryTerminal" + function + ";"
        "try {Assert-VercelPriorJournalHistoryTerminal}catch{};$setCalls"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
def test_recovery_only_rejects_prior_nonterminal_for_later_expected_date_without_mutation(
    tmp_path: Path,
) -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    function = (
        "function Assert-VercelPriorJournalHistoryTerminal"
        + publisher.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
            "function Assert-VercelJournalMatchesInvocation", 1
        )[0]
    )
    history = tmp_path / "history"
    dated = history / "2026-08-31"
    dated.mkdir(parents=True)
    (dated / journal.CANONICAL_JOURNAL_NAME).write_text("{}", encoding="utf-8")
    payload = _pre_payload()
    payload.update(_authorization_fields())
    payload_json = json.dumps(payload, separators=(",", ":")).replace("'", "''")
    aliases_json = json.dumps(payload["production_aliases"], separators=(",", ":")).replace(
        "'", "''"
    )
    history_text = str(history).replace("'", "''")
    state_text = str(tmp_path).replace("'", "''")
    command = (
        f"$journalHistoryRoot='{history_text}';$resolvedStateRoot='{state_text}';"
        "$resolvedExpectedMarketDate='2026-09-01';$RecoveryOnly=$true;"
        "$ProjectId='prj_test';$ProjectName='dawnstrike-command-center-x3';"
        "$ProviderScope='mattfrens-projects';"
        f"$allProductionAliases=@((ConvertFrom-Json '{aliases_json}')|ForEach-Object{{$_}});"
        f"$script:payload=ConvertFrom-Json '{payload_json}';$script:providerCalls=0;"
        "function Assert-VercelCompensatedArchivesValid {};"
        "function Invoke-VercelJournalTool {"
        "$copy=$script:payload|ConvertTo-Json -Depth 30|ConvertFrom-Json;"
        "$copy.candidate_market_date='2026-08-31';[pscustomobject]@{payload=$copy}};"
        "function Invoke-VercelPublicationCompensation {$script:providerCalls++};"
        + function
        + ";$message='';try{Assert-VercelPriorJournalHistoryTerminal}catch{"
        "$message=$_.Exception.Message};[pscustomobject]@{calls=$script:providerCalls;"
        "message=$message}|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "calls": 0,
        "message": (
            "RecoveryOnly found a nonterminal Vercel journal outside its exact ExpectedMarketDate."
        ),
    }


def test_recovery_only_v1_receipts_bind_complete_governed_provider_tuple() -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    starts = [
        match.start()
        for match in re.finditer(
            r'schema_version\s*=\s*["\']dawnstrike\.vercel_publication_recovery\.v1["\']',
            publisher,
        )
    ]
    assert len(starts) == 3
    expected_statuses = {
        "NO_NONTERMINAL_CURRENT_OPERATION",
        "ARCHIVED_COMPENSATED",
        "COMPENSATED",
    }
    observed_statuses: set[str] = set()
    for start in starts:
        block = publisher[start : start + 900]
        status = re.search(r'status\s*=\s*["\']([^"\']+)["\']', block)
        assert status is not None
        observed_statuses.add(status.group(1))
        for field in (
            "project_id",
            "project_name",
            "provider_scope",
            "production_aliases",
        ):
            assert re.search(rf"(?m)^\s*{field}\s*=", block), (status.group(1), field)
    assert observed_statuses == expected_statuses


def test_recovery_only_native_console_suppression_is_explicit_and_scoped() -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    admission = publisher.index("$bootstrapSource = Assert-VercelRecoveryBootstrapSource")
    restriction = publisher.index(
        "Native console replay suppression is restricted to recovery-only"
    )
    assert "[switch]$SuppressNativeConsoleReplay" in publisher[:admission]
    assert restriction < admission
    assert publisher.count("Invoke-DawnstrikeNativeProcess") == 2
    assert publisher.count("-SuppressConsoleReplay:$SuppressNativeConsoleReplay") == 2


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("schema", "phase", "consumed"),
    [
        (journal.SCHEMA, "COMPLETE", True),
        (journal.COMPENSATED_SCHEMA, "COMPENSATED", False),
    ],
)
def test_pinned_legacy_exception_is_consumed_only_by_successful_current_migration(
    tmp_path: Path, schema: str, phase: str, consumed: bool
) -> None:
    publisher = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    functions = (
        "function Assert-VercelPriorJournalHistoryTerminal"
        + publisher.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
            "function Assert-VercelJournalMatchesInvocation", 1
        )[0]
    )
    history = tmp_path / "history"
    dated = history / "2026-08-31"
    dated.mkdir(parents=True)
    (dated / "vercel-publication-operation.json").write_text("{}", encoding="utf-8")
    payload = _pre_payload()
    payload.update(_authorization_fields())
    payload["schema_version"] = schema
    payload["phase"] = phase
    payload["sequence"] = 2 if phase == "COMPLETE" else 3
    payload_json = json.dumps(payload, separators=(",", ":")).replace("'", "''")
    history_text = str(history).replace("'", "''")
    state_text = str(tmp_path).replace("'", "''")
    aliases_json = json.dumps(payload["production_aliases"], separators=(",", ":")).replace(
        "'", "''"
    )
    command = (
        f"$journalHistoryRoot='{history_text}';$resolvedStateRoot='{state_text}';"
        "$resolvedExpectedMarketDate='2026-09-01';$ProjectId='prj_test';"
        "$ProjectName='dawnstrike-command-center-x3';$ProviderScope='mattfrens-projects';"
        f"$allProductionAliases=@((ConvertFrom-Json '{aliases_json}')|ForEach-Object{{$_}});"
        f"$script:historyPayload=ConvertFrom-Json '{payload_json}';"
        "function Assert-VercelCompensatedArchivesValid {};"
        "function Invoke-VercelJournalTool {[pscustomobject]@{payload=$script:historyPayload}};"
        "function Invoke-VercelPublicationCompensation {throw 'unexpected compensation'};"
        + functions
        + ";Assert-VercelPriorJournalHistoryTerminal;"
        + "$blocked=$false;try{Assert-VercelPinnedLegacyRollbackAvailable}catch{$blocked=$true};"
        + "[pscustomobject]@{consumed=[bool]$script:pinnedLegacyRollbackConsumed;"
        + "blocked=$blocked}|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed == {"consumed": consumed, "blocked": consumed}


def test_complete_and_interrupted_recovery_repair_both_result_copies() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    repair = script.split("function Repair-VercelResultCopies", 1)[1].split(
        "function Assert-LowerHex64", 1
    )[0]
    assert "$resultPath" in repair and "$runtimeResultPath" in repair
    assert "Write-VercelResultAtomic -Payload $Payload" in repair
    assert script.count("Repair-VercelResultCopies -Payload") >= 2
    recovery = script.split("function Complete-VercelJournalRecovery", 1)[1].split(
        "$recoveryRetry", 1
    )[0]
    assert "New-VercelRecoveredResultPayload" in recovery
    assert "$complete.result_payload = $freshResult" in recovery
    recovered = script.split("function New-VercelRecoveredResultPayload", 1)[1].split(
        "if ($Promote -or $PrepublicationAuthorizationId)", 1
    )[0]
    assert "Recovered preview governed asset manifest" in recovered
    assert "foreach ($alias in @($Journal.production_aliases))" in recovered
    authorization_calls = [
        index
        for index in range(len(script))
        if script.startswith("Assert-GovernedPublicationAuthorization", index)
    ]
    recovery_guard = script.index(
        "if ($RecoveryOnly) {\n"
        '    throw "Recovery-only Vercel convergence reached a fresh publication path."'
    )
    assert any(
        recovery_guard < index < script.index("scripts\\build_vercel_public_stage.ps1")
        for index in authorization_calls
    )
    assert any(
        script.index("scripts\\build_vercel_public_stage.ps1")
        < index
        < script.index('Invoke-VercelProcess `\n            -Arguments @("promote"')
        for index in authorization_calls
    )
    existing_boundary = script.split("$existingJournal = Get-VercelPublicationJournal", 1)[1].split(
        "function Get-VercelGovernedAssetProof", 1
    )[0]
    assert (
        "prepublication_authorization_id -ne $PrepublicationAuthorizationId"
        not in existing_boundary
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("journal_id", "journal_url", "observed_id", "observed_url", "expected"),
    [
        (
            "dpl_clone_a",
            "https://dawnstrike-command-center-x3-clonea-mattfrens-projects.vercel.app",
            "dpl_clone_a",
            "https://dawnstrike-command-center-x3-clonea-mattfrens-projects.vercel.app",
            True,
        ),
        (
            "dpl_clone_a",
            "https://dawnstrike-command-center-x3-clonea-mattfrens-projects.vercel.app",
            "dpl_clone_b",
            "https://dawnstrike-command-center-x3-cloneb-mattfrens-projects.vercel.app",
            False,
        ),
        (
            None,
            None,
            "dpl_clone_b",
            "https://dawnstrike-command-center-x3-cloneb-mattfrens-projects.vercel.app",
            True,
        ),
    ],
)
def test_powershell_promotion_recovery_binds_recorded_clone_when_present(
    journal_id: str | None,
    journal_url: str | None,
    observed_id: str,
    observed_url: str,
    expected: bool,
) -> None:
    publisher = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    helpers = (
        "function Test-VercelObjectProperty"
        + publisher.split("function Test-VercelObjectProperty", 1)[1].split(
            "function Set-VercelAlias", 1
        )[0]
    )
    normalize = (
        "function Normalize-VercelDeploymentUrl"
        + publisher.split("function Normalize-VercelDeploymentUrl", 1)[1].split(
            "function Assert-VercelPriorAliasSnapshotsCurrent", 1
        )[0]
    )
    function = (
        "function Test-VercelPromotedCandidateSetMatchesJournal"
        + publisher.split("function Test-VercelPromotedCandidateSetMatchesJournal", 1)[1].split(
            "function Get-VercelGovernedAssetProof", 1
        )[0]
    )

    def literal(value: str | None) -> str:
        return "$null" if value is None else "'" + value.replace("'", "''") + "'"

    command = (
        helpers
        + normalize
        + function
        + "$allProductionAliases=@('https://alias-a.vercel.app','https://alias-b.vercel.app');"
        + "$ProjectName='dawnstrike-command-center-x3';"
        + f"$script:observedId='{observed_id}';$script:observedUrl='{observed_url}';"
        + "function Get-VercelAliasObservation {param($Alias);"
        + "[pscustomobject]@{id=$script:observedId;url=$script:observedUrl}};"
        + "function Invoke-VercelJson {[pscustomobject]@{deployments=@([pscustomobject]@{"
        + "id=$script:observedId;target='production';meta=[pscustomobject]@{"
        + "action='promote';originalDeploymentId='dpl_preview'}})}};"
        + "$journal=[pscustomobject]@{candidate_preview_deployment_id='dpl_preview';"
        + f"promoted_deployment_id={literal(journal_id)};"
        + f"promoted_deployment_url={literal(journal_url)}}};"
        + "Test-VercelPromotedCandidateSetMatchesJournal -Journal $journal"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(expected)


def test_publisher_two_lock_handshake_precedes_history_and_provider_recovery() -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    helper = publisher.index("function Assert-VercelRuntimeActivationAbsent")
    helper_end = publisher.index("function Assert-VercelJournalBaseMatchesInvocation", helper)
    helper_source = publisher[helper:helper_end]
    assert "dawnstrike-runtime-activation.lock" in helper_source
    assert "Remove-Item" not in helper_source
    assert "Adopt" not in helper_source

    execution = publisher.index(
        "try {\nif (-not ($Promote or $RecoveryOnly))".replace(" or ", " -or ")
    )
    preview_acquire = publisher.index("Acquire-VercelPublicationLock", execution)
    preview_handshake = publisher.index("Assert-VercelRuntimeActivationAbsent", preview_acquire)
    production_acquire = publisher.index("Acquire-VercelPublicationLock", preview_handshake)
    production_handshake = publisher.index(
        "Assert-VercelRuntimeActivationAbsent", production_acquire
    )
    history = publisher.index("Assert-VercelPriorJournalHistoryTerminal", production_handshake)
    existing = publisher.index("$existingJournal = Get-VercelPublicationJournal", history)
    assert preview_acquire < preview_handshake < production_acquire
    assert production_acquire < production_handshake < history < existing


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell handshake contract")
def test_publisher_two_lock_handshake_blocks_any_runtime_activation_lock(
    tmp_path: Path,
) -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    function = (
        "function Assert-VercelRuntimeActivationAbsent"
        + publisher.split("function Assert-VercelRuntimeActivationAbsent", 1)[1].split(
            "function Assert-VercelJournalBaseMatchesInvocation", 1
        )[0]
    )
    state = str(tmp_path / "state").replace("'", "''")
    command = (
        function
        + f"$resolvedStateRoot='{state}';"
        + "function Assert-VercelContainedNonReparsePath {param($RootPath,$TargetPath)};"
        + "$locks=Join-Path $resolvedStateRoot 'locks';"
        + "New-Item -ItemType Directory -Path $locks -Force|Out-Null;"
        + "$clear=$false;try{Assert-VercelRuntimeActivationAbsent;$clear=$true}catch{};"
        + "$path=Join-Path $locks 'dawnstrike-runtime-activation.lock';"
        + "[IO.File]::WriteAllText($path,'malformed');"
        + "$blocked=$false;try{Assert-VercelRuntimeActivationAbsent}catch{"
        + "$blocked=$_.Exception.Message -match 'runtime activation lock exists'};"
        + "[pscustomobject]@{clear=$clear;blocked=$blocked}|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "clear": True,
        "blocked": True,
    }


def test_compensated_terminal_dereferences_and_binds_receipt(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    prior = _pre_payload()
    prior.update(_authorization_fields())
    prior_path = root / "journal.json"
    prior_path.write_bytes(_seal(prior_path, prior))
    evidence = [
        {
            "alias": item["alias"],
            "expected_deployment_id": item["deployment_id"],
            "expected_deployment_url": item["deployment_url"],
            "observed_deployment_id": item["deployment_id"],
            "observed_deployment_url": item["deployment_url"],
            "restored": True,
            "health_status": item["health_status"],
            "readiness_status": item["readiness_status"],
            "readiness_http_status": item["readiness_http_status"],
            "source_sha": item["source_sha"],
            "source_tree": item["source_tree"],
            "source_manifest_sha256": item["source_manifest_sha256"],
            "build_manifest_sha256": item["build_manifest_sha256"],
            "release_manifest_sha256": item["release_manifest_sha256"],
            "artifact_proof": item["artifact_proof"],
            "rollback_contract": item["rollback_contract"],
        }
        for item in prior["prior_aliases"]
    ]
    compensation = {
        "schema_version": journal.COMPENSATION_SCHEMA,
        "status": "COMPENSATED",
        "operation": "vercel_publication",
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "promoted_deployment_id": None,
        "promoted_deployment_url": None,
        "prior_aliases": prior["prior_aliases"],
        "rollback_evidence": evidence,
        "rollback_status": "ROLLED_BACK",
        "failure_type": "hostile_test",
        "research_only": True,
        "broker_execution_enabled": False,
        "recorded_at_utc": "2026-08-31T12:00:00.000000Z",
    }
    compensation["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json(compensation)
    ).hexdigest()
    compensation_path = root / "outputs" / "compensation.json"
    compensation_path.parent.mkdir()
    compensation_path.write_bytes(journal.canonical_json(compensation))
    compensation_hash = hashlib.sha256(compensation_path.read_bytes()).hexdigest()
    tombstone = {
        "schema_version": "dawnstrike.daily_deployment_compensated.v1",
        "status": "COMPENSATED",
        "market_date": prior["candidate_market_date"],
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "compensation_sha256": compensation_hash,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result_path = root / prior["result_relative_path"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(journal.canonical_json(tombstone))
    terminal = dict(prior)
    terminal.update(
        {
            "schema_version": journal.COMPENSATED_SCHEMA,
            "phase": "COMPENSATED",
            "sequence": 3,
            "compensation_relative_path": "outputs/compensation.json",
            "compensation_sha256": compensation_hash,
            "result_payload": tombstone,
            "production_result_sha256": hashlib.sha256(
                journal.canonical_json(tombstone)
            ).hexdigest(),
            "prior_journal_file_sha256": hashlib.sha256(prior_path.read_bytes()).hexdigest(),
        }
    )
    source = root / "terminal-input.json"
    source.write_bytes(journal.canonical_json(terminal))
    journal.transition(source, prior_path, prior_path, state_root=root)
    validated = journal.validate(prior_path.read_bytes(), state_root=root, journal_path=prior_path)
    assert validated["phase"] == "COMPENSATED"
    result_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="result raw hash"):
        journal.validate(prior_path.read_bytes(), state_root=root, journal_path=prior_path)
    result_path.write_bytes(journal.canonical_json(tombstone))

    for field, hostile, message in (
        ("observed_deployment_id", "wrong-deployment", "observed deployment mismatch"),
        (
            "observed_deployment_url",
            "https://dawnstrike-command-center-x3-wrong-mattfrens-projects.vercel.app",
            "observed URL mismatch",
        ),
    ):
        hostile_compensation = json.loads(json.dumps(compensation))
        hostile_compensation["rollback_evidence"][0][field] = hostile
        unsigned = {
            key: value
            for key, value in hostile_compensation.items()
            if key != "receipt_self_sha256"
        }
        hostile_compensation["receipt_self_sha256"] = hashlib.sha256(
            journal.canonical_json(unsigned)
        ).hexdigest()
        with pytest.raises(ValueError, match=message):
            journal.validate_compensation(journal.canonical_json(hostile_compensation))

    tampered = dict(compensation)
    tampered["failure_type"] = "tampered"
    tampered["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json({k: v for k, v in tampered.items() if k != "receipt_self_sha256"})
    ).hexdigest()
    compensation_path.write_bytes(journal.canonical_json(tampered))
    with pytest.raises(ValueError, match="compensation receipt raw hash"):
        journal.validate(prior_path.read_bytes(), state_root=root, journal_path=prior_path)
