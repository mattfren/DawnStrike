from __future__ import annotations

import re
from pathlib import Path

BATCH_PATH = Path("scripts/run_alphaops_eod_full.bat")


def _batch_text() -> str:
    return BATCH_PATH.read_text(encoding="utf-8")


def test_full_eod_batch_contains_no_embedded_telegram_secret() -> None:
    batch = _batch_text()

    credential_assignment = re.compile(
        r"(?im)^\s*set\s+\"?(?:INTRADAY_)?TELEGRAM_(?:BOT_TOKEN|CHAT_ID)\s*="
    )
    telegram_bot_token = re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{20,}\b")

    assert credential_assignment.search(batch) is None
    assert telegram_bot_token.search(batch) is None
    assert "api.telegram.org/bot" not in batch.lower()
    assert "https://hooks." not in batch.lower()


def test_full_eod_batch_pins_one_market_date_across_the_fleet() -> None:
    batch = _batch_text()

    assert batch.count('set "RUN_DATE=%%I"') == 1
    assert "alpha-paper-reconcile" in batch
    assert "--market-date %RUN_DATE%" in batch
    assert "alpha-learn" in batch
    assert "call scripts\\run_paperops_fleet_eod.bat %RUN_DATE%" in batch


def test_full_eod_batch_preserves_fail_closed_alpha_truth_gates() -> None:
    batch = _batch_text()
    phase_markers = (
        "intraday_scanner.services.market_calendar",
        "alpha-paper-reconcile",
        "alpha-learn",
        "alpha-report",
        "run_paperops_fleet_eod.bat",
    )
    offsets = [batch.index(marker) for marker in phase_markers]
    assert offsets == sorted(offsets)

    assert 'if "%RECONCILE_EXIT%"=="2" (' in batch
    assert 'if not "%RECONCILE_EXIT%"=="0" (' in batch
    assert 'if "%LEARN_EXIT%"=="2" (' in batch
    assert "exit /b 2" in batch
    assert "exit /b 1" in batch
    assert "alpha-capture-outcomes" not in batch
    assert "daily-review" not in batch
    assert "trade-watch" not in batch


def test_full_eod_batch_requires_real_bars_or_explicit_no_trade() -> None:
    batch = _batch_text()

    assert "DAWNSTRIKE_ALPHAOPS_EOD_BARS_CSV" in batch
    assert (
        "data\\v2_autodata\\normalized\\canonical\\%RUN_DATE%_canonical_intraday.csv"
        in batch
    )
    assert (
        'if not defined BARS_ARG if exist "data\\v2_autodata\\normalized\\canonical"'
        in batch
    )
    assert "--bars-csv" in batch
    assert "no broker/order command" in batch.lower()
