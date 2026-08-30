from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.services.capture_operations import CapturePlan, CapturePlanError, plan_as_dict


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(tmp_path: Path, repo: Path) -> CapturePlan:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "membership_policy": "controls only",
                "point_in_time_membership": "research_control_only",
                "symbols": ["SPY", "IWM", "QQQ", "DIA", "TLT"],
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "exchange": "XNYS",
                "market_date": "2026-08-28",
                "exchange_session_id": "XNYS:2026-08-28:regular",
                "calendar_id": "us-equities-xnys-xnas-2026-2028.v1",
                "start_utc": "2026-08-28T13:30:00Z",
                "end_utc": "2026-08-28T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    entitlement = tmp_path / "entitlement.json"
    entitlement.write_text(
        json.dumps(
            {
                "entitlement": "alpaca-historical-sip-older-than-15-minutes",
                "proof_id": "operator-receipt-1",
                "provider": "alpaca",
                "feed": "sip",
                "probe_status": "PASS",
                "proven_endpoints": ["bars", "trades", "quotes"],
                "retention_allowed": True,
                "approved_plan": True,
                "research_only": True,
                "broker_execution": "disabled",
                "secret": "never-copy",
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "web_sources.yaml"
    source.write_text("sources: []\n", encoding="utf-8")
    env = tmp_path / "runtime.env"
    env.write_text("ALPACA_DATA_FEED=sip\n", encoding="utf-8")
    root = tmp_path / "external"
    return CapturePlan(
        mode="forward_observed",
        provider="alpaca",
        feed="sip",
        candidate_sha=subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip(),
        repo_root=repo,
        db_path=root / "db" / "staging.sqlite",
        evidence_root=root / "evidence",
        run_root=root / "runs",
        output_root=root / "output",
        symbols_manifest=manifest,
        symbols_manifest_sha256=_sha(manifest),
        expected_session=session,
        entitlement_receipt=entitlement,
        entitlement_receipt_sha256=_sha(entitlement),
        source_config=source,
        source_config_sha256=_sha(source),
        env_file=env,
        max_pages=4,
        retries=2,
    )


def test_plan_requires_delayed_sip_and_separate_external_roots(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    plan = _plan(tmp_path, repo)
    result = plan_as_dict(plan, now=datetime(2026, 8, 30, 12, tzinfo=UTC))
    assert result["status"] == "READY"
    assert result["symbols"] == ["SPY", "IWM", "QQQ", "DIA", "TLT"]
    assert result["broker_execution"] == "disabled"
    assert result["required_endpoints"] == [
        "bars",
        "trades",
        "quotes",
        "corporate_actions",
    ]
    assert result["full_microstructure_requested"] is True
    assert result["mode_evidence_root"].endswith("forward_observed")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("feed", "iex", "exactly sip"),
        ("provider", "massive", "Alpaca"),
    ],
)
def test_plan_rejects_feed_substitution_and_non_alpaca_provider(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    repo = Path(__file__).resolve().parents[1]
    plan = _plan(tmp_path, repo)
    object.__setattr__(plan, field, value)
    with pytest.raises(CapturePlanError, match=message):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_plan_rejects_active_state_output(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    plan = _plan(tmp_path, repo)
    object.__setattr__(plan, "output_root", Path(r"C:\r\dawnstrike-state\capture"))
    with pytest.raises(CapturePlanError, match="active state"):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_plan_rejects_recent_window(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    plan = _plan(tmp_path, repo)
    session = plan.expected_session
    session.write_text(
        json.dumps(
            {
                "exchange": "XNYS",
                "market_date": "2026-08-28",
                "exchange_session_id": "XNYS:2026-08-28:regular",
                "start_utc": "2026-08-28T19:00:00Z",
                "end_utc": "2026-08-28T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CapturePlanError, match="15 minutes old"):
        plan.validate(now=datetime(2026, 8, 28, 20, 5, tzinfo=UTC))


def test_plan_rejects_dirty_candidate_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    plan_root = tmp_path / "plan"
    plan_root.mkdir()
    plan = _plan(plan_root, repo)
    tracked.write_text("dirty\n", encoding="utf-8")

    with pytest.raises(CapturePlanError, match="not clean"):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_plan_rejects_unproven_entitlement_receipt(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    plan = _plan(tmp_path, repo)
    plan.entitlement_receipt.write_text(
        json.dumps({"entitlement": "claimed", "proof_id": "forged"}),
        encoding="utf-8",
    )
    object.__setattr__(plan, "entitlement_receipt_sha256", _sha(plan.entitlement_receipt))

    with pytest.raises(CapturePlanError, match="provider/feed identity"):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_capture_script_is_plan_only_without_execute() -> None:
    script = Path("scripts/capture_intraday_operations.py").read_text(encoding="utf-8")
    registration = Path("scripts/register_intraday_capture_task.ps1").read_text(encoding="utf-8")
    assert "--execute" in script
    assert "--include-trades" in script
    assert "--include-quotes" in script
    assert "--include-corporate-actions" in script
    assert "-Create" in registration
    assert "Register-ScheduledTask" in registration
    assert "feed substitution" in script or "feed must be exactly" in Path(
        "intraday_scanner/services/capture_operations.py"
    ).read_text(encoding="utf-8")
