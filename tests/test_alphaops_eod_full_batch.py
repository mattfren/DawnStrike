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

    assert batch.count("set RUN_DATE=%%I") == 1
    assert (
        'if not "!PAPEROPS_CURRENT_DATE!"=="%RUN_DATE%" (' in batch
    )
    assert (
        "paper_ops run-day --date %RUN_DATE% --mode forward "
        '--output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"'
    ) in batch
    assert (
        "strategy-fleet-report --db-path data\\shadow_real.sqlite "
        '--paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%" '
        "--out-dir outputs\\strategy_fleet --start %RUN_DATE% --end %RUN_DATE%"
    ) in batch
    assert (
        "strategy-fleet-telegram --date %RUN_DATE% "
        "--db-path data\\shadow_real.sqlite "
        '--paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%" '
        "--fleet-report outputs\\strategy_fleet\\strategy_fleet_report.json "
        "--notify telegram --max-attempts 3"
    ) in batch


def test_full_eod_batch_preserves_fail_closed_fleet_digest_gates() -> None:
    batch = _batch_text()
    phase_markers = (
        "call :RUN_PAPEROPS_FORWARD_WITH_RETRY",
        "Establishing pre-shadow PaperOps truth gates.",
        "paper_ops shadow-init",
        "paper_ops shadow-run",
        "Rebuilding truth after shadow evidence writes.",
        "paper_ops blotter",
        "paper_ops verify-blotter",
        "paper_ops challenger-evaluate",
        "paper_ops evidence",
        "strategy-fleet-report",
        "set PAPEROPS_DIGEST_READY=1",
        'if "%PAPEROPS_DIGEST_READY%"=="1" (',
        "strategy-fleet-telegram",
    )
    offsets = [batch.index(marker) for marker in phase_markers]
    assert offsets == sorted(offsets)

    gates = (
        "PAPEROPS_FORWARD_OK",
        "PAPEROPS_VERIFY_OK",
        "PAPEROPS_SOURCE_TRUTH_OK",
        "PAPEROPS_SHADOW_OK",
        "POST_SHADOW_TRUTH_OK",
        "PAPEROPS_BLOTTER_OK",
        "CHALLENGER_EVAL_OK",
        "PAPEROPS_EVIDENCE_OK",
        "FLEET_REPORT_OK",
    )
    for gate in gates:
        assert (
            f'if not "%{gate}%"=="1" set PAPEROPS_DIGEST_READY=0' in batch
        )

    assert batch.count("strategy-fleet-report") == 1
    assert batch.count("strategy-fleet-telegram") == 1
    assert (
        "PaperOps fleet Telegram blocked because forward, shadow, challenger, "
        "truth, evidence, or fleet-report gates did not complete."
    ) in batch
    assert "exit /b %EXITCODE%" in batch


def test_full_eod_batch_uses_one_configured_root_for_every_paperops_phase() -> None:
    batch = _batch_text()

    assert (
        'if not defined DAWNSTRIKE_PAPER_OPS_ROOT set '
        '"DAWNSTRIKE_PAPER_OPS_ROOT=data\\v2_paper_ops_live"'
    ) in batch
    paperops_lines = [
        line.strip()
        for line in batch.splitlines()
        if line.strip().startswith("py -m intraday_scanner.v2.paper_ops ")
    ]
    assert paperops_lines
    assert all(
        '--output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"' in line
        for line in paperops_lines
    )

    commands = [line.split()[3] for line in paperops_lines]
    assert commands.count("run-day") == 1
    assert commands.count("reconcile") == 2
    assert commands.count("verify-calendar") == 2
    assert commands.count("rebuild-ledger") == 2
    assert commands.count("verify-source-bars") == 2
    assert commands.count("shadow-init") == 1
    assert commands.count("shadow-run") == 1
    assert commands.count("blotter") == 1
    assert commands.count("verify-blotter") == 1
    assert commands.count("challenger-evaluate") == 1
    assert commands.count("evidence") == 1
