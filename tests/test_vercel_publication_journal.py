"""Hostile, network-free checks for the Vercel publication journal contract."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import vercel_publication_journal as journal


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
        "production_aliases": aliases,
        "candidate_preview_url": "https://preview.example.vercel.app",
        "candidate_preview_deployment_id": "dpl_preview",
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "candidate_market_date": "2026-08-31",
        "candidate_build_id": "c" * 20,
        "candidate_build_sha": "c" * 64,
        "candidate_manifest_sha256": "d" * 64,
        "candidate_package_manifest_sha256": "e" * 64,
        "prior_aliases": [
            {
                "alias": alias,
                "deployment_id": f"prior-{i}",
                "deployment_url": f"https://prior-{i}.vercel.app",
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
    }
    post = dict(pre_payload)
    post.update(
        {
            "phase": "POST_ALIASES",
            "sequence": 1,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": "https://promoted.example.vercel.app",
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
            "promoted_deployment_url": "https://promoted.example.vercel.app",
            "production_result_sha256": hashlib.sha256(
                journal.canonical_json(result)
            ).hexdigest(),
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
            "promoted_deployment_url": "https://promoted.example.vercel.app",
        }
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
        "promoted_deployment_id": payload["promoted_deployment_id"],
        "production_deployment_id": payload["promoted_deployment_id"],
        "vercel_source_manifest_sha256": payload["candidate_manifest_sha256"],
        "vercel_package_manifest_sha256": payload["candidate_package_manifest_sha256"],
        "allow_degraded": False,
        "promoted": True,
        "live_trading_enabled": False,
        "research_only": True,
        "status": "PRODUCTION_VERIFIED",
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
    journal.acquire_lock(
        replacement_source, owner_id="owner-b", pid=os.getpid(), **kwargs
    )
    replacement_raw = replacement_source.read_bytes()
    journal.release_lock(
        replacement_source, state_root=root, owner_id="owner-b", pid=os.getpid()
    )

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
    base_assertion = "Assert-VercelJournalBaseMatchesInvocation -Journal $existingJournal"
    assert base_assertion in script
    assert script.index(base_assertion) < script.index("$candidateLive =")
    function = script.split("function Assert-VercelJournalBaseMatchesInvocation", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    for binding in (
        "candidate_source_sha",
        "candidate_source_tree",
        "candidate_market_date",
        "expected_market_date",
        "project_id",
        "project_name",
        "prepublication_authorization_id",
        "daily_ledger_authorization_id",
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
    terminal = dict(prior)
    terminal.update(
        {
            "schema_version": journal.COMPENSATED_SCHEMA,
            "phase": "COMPENSATED",
            "sequence": 3,
            "compensation_relative_path": "outputs/compensation.json",
            "compensation_sha256": hashlib.sha256(compensation_path.read_bytes()).hexdigest(),
            "prior_journal_file_sha256": hashlib.sha256(prior_path.read_bytes()).hexdigest(),
        }
    )
    source = root / "terminal-input.json"
    source.write_bytes(journal.canonical_json(terminal))
    journal.transition(source, prior_path, prior_path, state_root=root)
    validated = journal.validate(
        prior_path.read_bytes(), state_root=root, journal_path=prior_path
    )
    assert validated["phase"] == "COMPENSATED"

    tampered = dict(compensation)
    tampered["failure_type"] = "tampered"
    tampered["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json({k: v for k, v in tampered.items() if k != "receipt_self_sha256"})
    ).hexdigest()
    compensation_path.write_bytes(journal.canonical_json(tampered))
    with pytest.raises(ValueError, match="compensation receipt raw hash"):
        journal.validate(prior_path.read_bytes(), state_root=root, journal_path=prior_path)
