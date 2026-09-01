from pathlib import Path

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService


def test_daily_finalize_repeated_fixed_run_is_logically_idempotent(tmp_path: Path) -> None:
    service = DailyFinalizeService(
        tmp_path / "idempotent.sqlite", tmp_path / "public", release_sha="a" * 40
    )
    first = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")
    second = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")
    assert first["run_id"] == second["run_id"]
    for key in (
        "status",
        "http_status",
        "input_hash_sha256",
        "payload_sha256",
        "calendar_payload_sha256",
        "publication_set_sha256",
    ):
        assert first["readiness"][key] == second["readiness"][key]
    assert first["publication_set"]["publication_set_sha256"] == (
        second["publication_set"]["publication_set_sha256"]
    )
    assert (
        second["readiness"]["daily_run"]["latest_stage_statuses"]["readiness"][
            "attempt_no"
        ]
        == 2
    )


def test_daily_finalize_run_id_is_market_date_keyed(tmp_path: Path) -> None:
    service = DailyFinalizeService(
        tmp_path / "date-keyed.sqlite", tmp_path / "public", release_sha="a" * 40
    )

    first = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")
    second = service.run(market_date="2026-07-29", now="2026-07-30T01:00:00+00:00")
    other_day = service.run(market_date="2026-07-30", now="2026-07-30T21:00:00+00:00")

    assert first["run_id"] == second["run_id"]
    assert first["run_id"] != other_day["run_id"]
