from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import intraday_scanner.services.strategy_challenger_evaluation_service as evaluation_service
from intraday_scanner.services.strategy_challenger_evaluation_service import (
    LOCAL_EVIDENCE_MANIFEST,
    LOCAL_EVIDENCE_MANIFEST_SCHEMA,
    StrategyChallengerEvidenceError,
    build_challenger_registry,
    canonical_hash,
    run_strategy_challenger_weekly_adapter,
)

SHA = "a" * 40
MARKET_DATE = "2026-08-30"


def _finalize(*, ready: bool = True) -> dict[str, object]:
    return {
        "status": "READY" if ready else "BLOCKED",
        "ready": ready,
        "run_id": "daily-1",
        "market_date": MARKET_DATE,
        "release_sha": SHA,
        "publication_identity_ready": ready,
    }


def _manifest(
    root: Path, *, decision: dict[str, object] | None = None, holdout: bool = False
) -> None:
    source = {
        "source_id": "fixture",
        "calendar": {"market_date": MARKET_DATE, "is_session": True},
        "holdout": {"market_dates": [MARKET_DATE] if holdout else []},
        "configuration": {"policy": "frozen"},
    }
    window = {"date": MARKET_DATE}
    payload: dict[str, object] = {
        "schema_version": LOCAL_EVIDENCE_MANIFEST_SCHEMA,
        "market_date": MARKET_DATE,
        "code_sha": SHA,
        "window": window,
        "window_hash_sha256": canonical_hash(window),
        "calendar": source["calendar"],
        "input_hashes": {},
        "source_manifest": source,
        "source_manifest_hash_sha256": canonical_hash(source),
    }
    if decision is not None:
        path = root / "decisions.json"
        path.write_text(json.dumps([decision]), encoding="utf-8")
        payload["prospective_decisions_path"] = path.name
        payload["input_hashes"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    (root / LOCAL_EVIDENCE_MANIFEST).write_text(json.dumps(payload), encoding="utf-8")


def _decision(*, holdout: bool = False) -> dict[str, object]:
    entry = build_challenger_registry()[0]
    source = {
        "source_id": "fixture",
        "calendar": {"market_date": MARKET_DATE, "is_session": True},
        "holdout": {"market_dates": [MARKET_DATE] if holdout else []},
        "configuration": {"policy": "frozen"},
    }
    return {
        "decision_id": "decision-1",
        "challenger_id": entry["challenger_id"],
        "strategy_id": entry["challenger_id"],
        "strategy_version": entry["challenger_version"],
        "market_date": MARKET_DATE,
        "decision_at": f"{MARKET_DATE}T14:00:00Z",
        "configuration_hash_sha256": entry["configuration_hash_sha256"],
        "source_lineage_hash_sha256": canonical_hash(source),
        "source_manifest_hash_sha256": canonical_hash(source),
        "code_sha": SHA,
        "window_hash_sha256": canonical_hash({"date": MARKET_DATE}),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_missing_authenticated_evidence_is_immutable_non_evaluable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.verify_daily_finalize_receipt.verify", lambda *args: _finalize())
    first = run_strategy_challenger_weekly_adapter(
        db_path=tmp_path / "db.sqlite",
        state_root=tmp_path,
        market_date=MARKET_DATE,
        code_sha=SHA,
    )
    second = run_strategy_challenger_weekly_adapter(
        db_path=tmp_path / "db.sqlite",
        state_root=tmp_path,
        market_date=MARKET_DATE,
        code_sha=SHA,
    )
    assert first["status"].startswith("NOT_EVALUABLE")
    assert first["metrics"] is None
    assert first["receipt_inserted"] is False
    assert second["receipt_inserted"] is True
    assert len(first["challengers"]) == 9


def test_forged_manifest_hash_fails_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.verify_daily_finalize_receipt.verify", lambda *args: _finalize())
    _manifest(tmp_path)
    path = tmp_path / LOCAL_EVIDENCE_MANIFEST
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["window_hash_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StrategyChallengerEvidenceError, match="window hash"):
        run_strategy_challenger_weekly_adapter(
            db_path=tmp_path / "db.sqlite",
            state_root=tmp_path,
            market_date=MARKET_DATE,
            code_sha=SHA,
        )


def test_retrospective_decision_and_duplicate_identity_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.verify_daily_finalize_receipt.verify", lambda *args: _finalize())
    decision = _decision()
    decision["after_cost_return_pct"] = 1.0
    _manifest(tmp_path, decision=decision)
    with pytest.raises(StrategyChallengerEvidenceError, match="retrospective"):
        run_strategy_challenger_weekly_adapter(
            db_path=tmp_path / "db.sqlite",
            state_root=tmp_path,
            market_date=MARKET_DATE,
            code_sha=SHA,
        )


def test_duplicate_decision_identity_fails_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.verify_daily_finalize_receipt.verify", lambda *args: _finalize())
    decision = _decision()
    source = tmp_path / "decisions.json"
    source.write_text(json.dumps([decision, decision]), encoding="utf-8")
    # Build a valid manifest around the intentionally duplicated source.
    _manifest(tmp_path)
    payload = json.loads((tmp_path / LOCAL_EVIDENCE_MANIFEST).read_text(encoding="utf-8"))
    payload["prospective_decisions_path"] = source.name
    payload["input_hashes"] = {source.name: hashlib.sha256(source.read_bytes()).hexdigest()}
    (tmp_path / LOCAL_EVIDENCE_MANIFEST).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StrategyChallengerEvidenceError, match="duplicate"):
        run_strategy_challenger_weekly_adapter(
            db_path=tmp_path / "db.sqlite",
            state_root=tmp_path,
            market_date=MARKET_DATE,
            code_sha=SHA,
        )


def test_finalize_gate_is_required_before_writing_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scripts.verify_daily_finalize_receipt.verify", lambda *args: _finalize(ready=False)
    )
    with pytest.raises(StrategyChallengerEvidenceError, match="Daily Finalize"):
        run_strategy_challenger_weekly_adapter(
            db_path=tmp_path / "db.sqlite",
            state_root=tmp_path,
            market_date=MARKET_DATE,
            code_sha=SHA,
        )
    assert not (tmp_path / "outputs" / "strategy_challenger_evaluation").exists()


def test_authenticated_inputs_use_existing_evaluators_but_gate_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(holdout=True)
    _manifest(tmp_path, decision=decision, holdout=True)
    closed = {"record_id": "closed-1", "decision_id": "decision-1"}
    closed_path = tmp_path / "closed.json"
    closed_path.write_text(json.dumps([closed]), encoding="utf-8")
    manifest_path = tmp_path / LOCAL_EVIDENCE_MANIFEST
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["closed_fill_truth_path"] = closed_path.name
    payload["input_hashes"][closed_path.name] = hashlib.sha256(
        closed_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        "scripts.verify_daily_finalize_receipt.verify", lambda *args: _finalize()
    )
    monkeypatch.setattr(
        evaluation_service,
        "has_authenticated_committed_fill_truth",
        lambda row: True,
    )
    called = {"value": False}

    def fake_evaluator(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["value"] = True
        return {
            "weekly_purged_walk_forward": {
                "challengers": [
                    {
                        "challenger_id": item["challenger_id"],
                        "overall_paired_metrics": {
                            "status": "BLOCKED_INSUFFICIENT_PAIRED_OOS"
                        },
                    }
                    for item in build_challenger_registry()
                ]
            },
            "prospective_shadow": {},
        }

    monkeypatch.setattr(evaluation_service, "run_strategy_challenger_evaluation", fake_evaluator)
    result = run_strategy_challenger_weekly_adapter(
        db_path=tmp_path / "db.sqlite",
        state_root=tmp_path,
        market_date=MARKET_DATE,
        code_sha=SHA,
    )
    assert called["value"] is True
    assert result["status"] == "NOT_EVALUABLE_SAMPLE_OR_SESSION_MINIMUM_MISSING"
    assert result["metrics"] is None


def test_weekly_script_invokes_adapter_after_finalize_gate() -> None:
    script = Path("scripts/run_alphaops_weekly_training.ps1").read_text(encoding="utf-8")
    assert "strategy-challenger-evaluate-weekly" in script
    assert "--state-root" in script
    assert "--code-sha" in script
    assert script.index("verify_daily_finalize_receipt.py") < script.index(
        "strategy-challenger-evaluate-weekly"
    )
    assert "eligibility booleans" in script
