from pathlib import Path

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService


def test_finalize_writes_stage_and_non_ready_empty_state(tmp_path: Path) -> None:
    result = DailyFinalizeService(tmp_path / "daily.sqlite", tmp_path / "public").run(
        market_date="2026-07-29", now="2026-07-29T21:00:00+00:00"
    )
    assert result["readiness"]["status"] == "not_ready"
    assert result["readiness"]["http_status"] == 503
    assert (tmp_path / "public" / "stage-manifest.json").exists()
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
