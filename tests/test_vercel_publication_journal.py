"""Hostile, network-free checks for the Vercel publication journal contract."""

import hashlib
import json
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
    pre.write_bytes(_seal(pre, _pre_payload()))
    prior_hash = hashlib.sha256(pre.read_bytes()).hexdigest()
    result = {"status": "PRODUCTION_VERIFIED", "source_sha": "a" * 40}
    post = _pre_payload()
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
