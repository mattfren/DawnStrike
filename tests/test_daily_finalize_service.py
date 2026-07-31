import json
from pathlib import Path

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService
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
