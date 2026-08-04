from __future__ import annotations

from pathlib import Path


def test_scenario_monitor_uses_a_durable_watermark_not_a_clock_window() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_monitor.ps1"
    ).read_text(encoding="utf-8")

    assert "scenario_monitor_watermark.json" in script
    assert "Get-ScenarioMonitorWatermark" in script
    assert "Save-ScenarioMonitorWatermark" in script
    assert "AddMinutes(-10)" not in script
    assert "$scenarioSince = Get-ScenarioMonitorWatermark" in script
    assert "else {\n                Save-ScenarioMonitorWatermark" in script


def test_scenario_eod_closes_then_finalizes_the_paper_challenger() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_eod.ps1"
    ).read_text(encoding="utf-8")

    close_at = script.index('"scenario-close"')
    finalize_at = script.index('"scenario-finalize"')
    paperops_at = script.index('"paperops_init-$MarketDate"')
    assert close_at < finalize_at < paperops_at
    assert 'Write-Stage -Name scenario_finalization -Status COMPLETE' in script
    assert 'scenario_finalization_failed' in script
