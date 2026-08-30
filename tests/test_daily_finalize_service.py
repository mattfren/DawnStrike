import json
from pathlib import Path

from intraday_scanner.errors import ConfigError
from intraday_scanner.services.daily_finalize_service import (
    DailyFinalizeService,
    _reconciliation_gate,
)
from intraday_scanner.services.daily_run_service import record_daily_stage


def test_finalize_writes_stage_and_non_ready_empty_state(tmp_path: Path) -> None:
    result = DailyFinalizeService(tmp_path / "daily.sqlite", tmp_path / "public").run(
        market_date="2026-07-29", now="2026-07-29T21:00:00+00:00"
    )
    assert result["readiness"]["status"] == "not_ready"
    assert result["readiness"]["http_status"] == 503
    assert (tmp_path / "public" / "stage-manifest.json").exists()
    assert (tmp_path / "public" / "data" / "calendar.json").exists()
    assert (tmp_path / "public" / "data" / "calendar.json.manifest.json").exists()
    publication_set = json.loads(
        (tmp_path / "public" / "data" / "publication-set.json").read_text(
            encoding="utf-8"
        )
    )
    assert publication_set["performance_payload_sha256"] == result["readiness"][
        "payload_sha256"
    ]
    assert publication_set["calendar_payload_sha256"] == result["readiness"][
        "calendar_payload_sha256"
    ]
    assert publication_set["publication_set_sha256"] == result["readiness"][
        "publication_set_sha256"
    ]
    stages = result["stage_manifest"]["stages"]
    assert [stage["stage"] for stage in stages] == [
        "source_collection",
        "candidate_normalization",
        "selection",
        "delivery",
        "paper_fills",
        "outcome_capture",
        "paper_reconciliation",
        "canonical_performance",
        "public_snapshot",
        "public_calendar",
        "preview_deployment",
        "production_promotion",
        "readiness",
    ]
    assert all(
        {
            "stage_version",
            "input_hash_sha256",
            "output_hash_sha256",
            "started_at",
            "completed_at",
            "status",
            "attempt_count",
            "warnings",
            "error",
            "next_action",
        }
        <= stage.keys()
        for stage in stages
    )
    assert {
        "data/performance.json",
        "data/performance.json.manifest.json",
        "data/calendar.json",
        "data/calendar.json.manifest.json",
        "data/publication-set.json",
    } <= set(result["stage_manifest"]["artifacts"])


def test_finalize_exposes_failed_shared_upstream_stage(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    db_path = state / "daily.sqlite"
    release_sha = "f" * 40
    day = "2026-07-31"
    for stage in (
        "morning_collection",
        "ranking_delivery",
        "intraday_monitor",
    ):
        record_daily_stage(
            db_path=db_path,
            market_date=day,
            stage_name=stage,
            status="COMPLETE",
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
        )
    record_daily_stage(
        db_path=db_path,
        market_date=day,
        stage_name="eod_outcome_capture",
        status="TERMINAL_MISSING",
        runtime_root=runtime,
        state_root=state,
        release_sha=release_sha,
        exit_code=2,
        error_code="providers_exhausted",
        error_detail="No sourced close.",
    )

    result = DailyFinalizeService(
        db_path,
        tmp_path / "public",
        runtime_root=runtime,
        state_root=state,
        release_sha=release_sha,
    ).run(market_date=day, now="2026-07-31T22:00:00+00:00")

    assert result["status"] == "DEGRADED"
    assert result["upstream_status"] == "failed"
    run = result["readiness"]["daily_run"]["run"]
    assert run["failed_stage"] == "eod_outcome_capture"
    assert run["failure_reason"] == "No sourced close."


def test_finalize_does_not_retry_terminal_configuration_failure(
    tmp_path: Path, monkeypatch
) -> None:
    calls = 0

    def invalid_configuration(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ConfigError("provider configuration is invalid")

    monkeypatch.setattr(
        "intraday_scanner.services.daily_finalize_service.CanonicalPerformanceService.reconcile",
        invalid_configuration,
    )
    result = DailyFinalizeService(tmp_path / "terminal.sqlite", tmp_path / "public").run(
        market_date="2026-07-29", retry_limit=5
    )

    assert calls == 1
    assert result["status"] == "FAILED"
    assert result["retry_count"] == 0
    assert "ConfigError" in result["error_detail"]


def test_finalize_closed_session_records_terminal_no_trade_without_zero_truth(
    tmp_path: Path,
) -> None:
    result = DailyFinalizeService(
        tmp_path / "closed.sqlite",
        tmp_path / "public",
        runtime_root=tmp_path,
        state_root=tmp_path,
        release_sha="a" * 40,
    ).run(market_date="2026-08-29", now="2026-08-29T21:00:00+00:00")

    assert result["status"] == "SKIPPED_NOT_APPLICABLE"
    assert result["daily_run"]["run"]["status"] == "SKIPPED_NOT_APPLICABLE"
    funnel = result["no_trade_funnel"]
    assert funnel["status"] == "NO_TRADE"
    assert funnel["return_pct"] is None
    assert funnel["net_pnl"] is None
    assert funnel["picks"] is None
    assert result["stage_manifest"]["artifacts"] == []


def test_reconciliation_gate_allows_only_declared_historical_warnings() -> None:
    result = {
        "issue_count": 2,
        "issues": [
            {
                "issue_code": "missing_outcome",
                "severity": "warning",
                "market_date": "2026-07-31",
            },
            {
                "issue_code": "paper_ops_equity_pnl_component_mismatch",
                "severity": "warning",
                "market_date": "2026-08-04",
            },
        ],
        "paper_ops_reconciliation": {
            "state": "partial",
            "quarantined_count": 0,
            "source_return_field_mismatch_count": 0,
        },
        "daily": [
            {
                "market_date": "2026-08-04",
                "cohort": "official_forward_paper",
                "strategy_id": "alphaops_v5",
                "status": "NO_TRADE",
                "return_pct": 0.0,
                "no_trade_count": 1,
                "missing_outcome_count": 0,
                "quarantined_count": 0,
                "coverage": {"missing_count": 0},
            }
        ],
    }

    gate = _reconciliation_gate(result, market_date="2026-08-04")

    assert gate["ready"] is True
    assert gate["status"] == "ready_with_warnings"
    assert gate["warning_count"] == 2
    assert gate["blocking"] == []


def test_reconciliation_gate_blocks_current_missing_or_unknown_warning() -> None:
    result = {
        "issue_count": 1,
        "issues": [
            {
                "issue_code": "missing_outcome",
                "severity": "warning",
                "market_date": "2026-08-04",
            }
        ],
        "paper_ops_reconciliation": {
            "state": "complete",
            "quarantined_count": 0,
            "source_return_field_mismatch_count": 0,
        },
        "daily": [
            {
                "market_date": "2026-08-04",
                "cohort": "official_forward_paper",
                "strategy_id": "alphaops_v5",
                "status": "NO_TRADE",
                "return_pct": 0.0,
                "no_trade_count": 1,
                "missing_outcome_count": 0,
                "quarantined_count": 0,
                "coverage": {"missing_count": 0},
            }
        ],
    }

    gate = _reconciliation_gate(result, market_date="2026-08-04")
    assert gate["ready"] is False
    assert any("missing_outcome" in item for item in gate["blocking"])

    result["issues"][0]["issue_code"] = "unknown_warning"
    result["issues"][0]["market_date"] = "2026-07-31"
    unknown_gate = _reconciliation_gate(result, market_date="2026-08-04")
    assert unknown_gate["ready"] is False
    assert any("unknown_warning" in item for item in unknown_gate["blocking"])
