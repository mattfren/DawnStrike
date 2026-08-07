from __future__ import annotations

from pathlib import Path

from intraday_scanner.services.daily_run_service import DAILY_STAGE_ORDER


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
    assert "else {\n            Save-ScenarioMonitorWatermark" in script


def test_trade_watch_precedes_bounded_scenario_work() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_monitor.ps1"
    ).read_text(encoding="utf-8")

    assert script.index('"trade-watch"') < script.index('"scenario-monitor"')
    assert 'DAWNSTRIKE_SCENARIO_MAX_ARTICLES_PER_RUN = "3"' in script
    assert 'DAWNSTRIKE_SCENARIO_OPENAI_TIMEOUT_SECONDS = "30"' in script

    extractor = (
        Path(__file__).resolve().parents[1]
        / "intraday_scanner"
        / "ai"
        / "scenario_claim_extractor.py"
    ).read_text(encoding="utf-8")
    assert "max_retries=0" in extractor


def test_monitor_skips_empty_scenario_without_overwriting_core_truth() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_monitor.ps1"
    ).read_text(encoding="utf-8")

    assert "Test-DawnstrikeAlphaCycleArtifact" in script
    assert "$scenarioCandidateCount = [int64]$alphaArtifact.research_candidate_count" in script
    assert '"--symbols", $scenarioSymbols' in script
    assert "if ($scenarioCandidateCount -le 0)" in script
    assert "$coreExitCode = $exitCode" in script
    assert "Resolve-DawnstrikeCoreOptionalOutcome" in script
    assert "-ExitCode $coreExitCode" in script
    assert "$coreRecord = Write-MonitorStage" in script
    assert script.index("$coreRecord = Write-MonitorStage") < script.index(
        "$scenarioSince = Get-ScenarioMonitorWatermark"
    )
    assert 'scenarioErrorCode = "scenario_orchestration_failed"' in script
    assert "($scenarioStageRecordFailed -or $coreRecord.exit_code -ne 0)" in script
    assert "exit $outcome.final_exit_code" in script


def test_monitor_normalizes_closed_session_receipt_failure_to_exit_two() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_monitor.ps1"
    ).read_text(encoding="utf-8")

    assert 'exit $(if ($record.exit_code -eq 0) { 0 } else { 2 })' in script


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
    assert '-Name scenario_finalization' in script
    assert '-NotRequired' in script


def test_eod_gates_official_reconciliation_on_exact_no_trade_truth() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_eod.ps1"
    ).read_text(encoding="utf-8")

    capture_at = script.index('"alpha-capture-outcomes"')
    gap_at = script.index('"outcome-gap"')
    gate_at = script.index('"alpha-eod-gate"')
    reconcile_at = script.index('"alpha-paper-reconcile"')
    assert capture_at < gap_at < gate_at < reconcile_at
    assert "$officialOutcomesRequired = [bool]$gatePayload.official_outcomes_required" in script
    assert "elseif (-not $officialOutcomesRequired)" in script
    assert '-Status SKIPPED_NOT_APPLICABLE' in script
    assert 'if ($learningStageExit -eq 0 -and -not $alphaLearningRequired)' in script


def test_eod_renders_calendar_after_truth_verification() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_eod.ps1"
    ).read_text(encoding="utf-8")

    assert script.index('"verify-calendar"') < script.index('"calendar-view"')


def test_morning_runs_optional_scenario_for_research_candidates_not_only_final_picks() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_morning.ps1"
    ).read_text(encoding="utf-8")

    assert "Test-DawnstrikeAlphaCycleArtifact" in script
    assert "$scenarioCandidateCount = [int64]$alphaArtifact.research_candidate_count" in script
    assert '$scenarioSymbols = [string]::Join(",", @($alphaArtifact.research_symbols))' in script
    assert '"--symbols", $scenarioSymbols' in script
    assert 'if ($scenarioCandidateCount -le 0)' in script
    assert '-Status "SKIPPED_NOT_APPLICABLE"' in script
    assert '$coreStageExit = $stageExit' in script
    assert "Resolve-DawnstrikeMorningOutcome" in script
    assert '-ExitCode $coreStageExit' in script
    assert 'exit $outcome.final_exit_code' in script
    assert script.rindex('foreach ($stage in @("morning_collection", "ranking_delivery"))') < (
        script.index("$outcome = Resolve-DawnstrikeMorningOutcome")
    )


def test_morning_runs_cited_openai_research_only_for_data_ineligible_runs() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_alphaops_morning.ps1"
    ).read_text(encoding="utf-8")

    assert "$selectionOutcome = [string]$alphaArtifact.selection_outcome" in script
    assert "DAWNSTRIKE_INDETERMINATE_RESEARCH_ENABLED" in script
    assert '$selectionOutcome -ne "data_ineligible"' in script
    assert '"indeterminate-research"' in script
    assert '"--symbols", $scenarioSymbols' in script
    assert '"--selection-outcome", $selectionOutcome' in script
    assert '"--out", $indeterminateResearchPath' in script
    assert '-Name "indeterminate_research"' in script
    assert '"indeterminate_research_failed"' in script
    assert "indeterminate_research" in DAILY_STAGE_ORDER
