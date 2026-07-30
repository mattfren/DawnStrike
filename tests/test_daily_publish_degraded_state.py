import json
from pathlib import Path

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService


def test_empty_publish_is_not_ready_not_green(tmp_path: Path) -> None:
    output = tmp_path / "public"
    DailyFinalizeService(tmp_path / "degraded.sqlite", output).run(market_date="2026-07-29")
    readiness = json.loads((output / "readiness.json").read_text(encoding="utf-8"))
    assert readiness["status"] == "not_ready"
    assert readiness["http_status"] == 503
