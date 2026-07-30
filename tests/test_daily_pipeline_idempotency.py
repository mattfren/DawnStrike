from pathlib import Path

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService


def test_daily_finalize_repeated_fixed_run_is_logically_idempotent(tmp_path: Path) -> None:
    service = DailyFinalizeService(tmp_path / "idempotent.sqlite", tmp_path / "public")
    first = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")
    second = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")
    assert first["run_id"] == second["run_id"]
    assert first["readiness"] == second["readiness"]
