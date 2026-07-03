from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from intraday_scanner.v2.audit import (
    CodeLineage,
    DataLineage,
    ExecutionAssumptions,
    FeeAssumptions,
    ReportManifest,
    ReportType,
    RunManifest,
    RunType,
    SlippageAssumptions,
)
from intraday_scanner.v2.contracts import (
    AssetClass,
    BacktestSummary,
    Bar,
    BarBatch,
    DataSnapshot,
    DataSourceId,
    DataValidationIssue,
    DataValidationReport,
    DataValidationSeverity,
    DataValidationStatus,
    DecisionCard,
    HistoricalReturnSummary,
    OutcomeLabel,
    PerformanceMetric,
    PositionSizingInput,
    PositionSizingResult,
    ReportArtifact,
    RiskConfig,
    ScanCandidate,
    ScanResult,
    ScoreComponent,
    SetupScore,
    SignalDirection,
    SignalEvidence,
    SignalStatus,
    StrategyId,
    StrategyVersion,
    Symbol,
    Timeframe,
    TradeOutcome,
    TradePlan,
)

NOW = datetime(2026, 6, 27, 14, 30, tzinfo=timezone.utc)


def _source_id() -> DataSourceId:
    return DataSourceId("manual_fixture")


def _symbol() -> Symbol:
    return Symbol("NOVA", AssetClass.EQUITY, exchange="NASDAQ")


def _strategy_id() -> StrategyId:
    return StrategyId("legacy_signal_engine_v3")


def _strategy_version() -> StrategyVersion:
    return StrategyVersion("dawnstrike-signal-engine-v3")


def _bar() -> Bar:
    return Bar(
        symbol=_symbol(),
        timeframe=Timeframe.MINUTE_1,
        timestamp=NOW,
        open_price=Decimal("10.10"),
        high_price=Decimal("10.80"),
        low_price=Decimal("10.00"),
        close_price=Decimal("10.55"),
        volume=125000,
        source_id=_source_id(),
    )


def _scan_candidate() -> ScanCandidate:
    score = SetupScore(
        total=Decimal("87.50"),
        grade="A",
        components=(
            ScoreComponent(
                name="liquidity",
                value=Decimal("22.50"),
                max_value=Decimal("25"),
                weight=Decimal("1"),
            ),
        ),
    )
    evidence = SignalEvidence(
        evidence_id="evidence-1",
        label="premarket_volume",
        summary="Premarket liquidity is high for the fixture.",
        source_refs=("snapshot:sample",),
        confidence=Decimal("0.82"),
    )
    return ScanCandidate(
        candidate_id="candidate-1",
        symbol=_symbol(),
        direction=SignalDirection.LONG,
        status=SignalStatus.WATCHLIST,
        strategy_id=_strategy_id(),
        strategy_version=_strategy_version(),
        setup_score=score,
        evidence=(evidence,),
        generated_at=NOW,
        rank=1,
        entry_trigger=Decimal("10.90"),
        invalidation_level=Decimal("9.75"),
        target_price=Decimal("12.00"),
        risk_flags=("fixture_only",),
    )


def _risk_config() -> RiskConfig:
    return RiskConfig(
        config_id="risk-default",
        max_position_pct=Decimal("0.05"),
        max_daily_loss_pct=Decimal("0.02"),
        max_open_positions=3,
        hard_block_codes=("live_execution_disabled",),
    )


def _position_result() -> PositionSizingResult:
    return PositionSizingResult(
        result_id="size-1",
        request_id="size-request-1",
        allowed=True,
        quantity=10,
        notional=Decimal("109.00"),
        risk_amount=Decimal("11.50"),
    )


def _execution_assumptions() -> ExecutionAssumptions:
    return ExecutionAssumptions(
        assumption_id="research-only",
        research_only=True,
        order_type="none",
        fill_model="paper_midpoint",
    )


def _fee_assumptions() -> FeeAssumptions:
    return FeeAssumptions(
        model_id="zero-commission-plus-regulatory",
        commission_per_trade=Decimal("0"),
        regulatory_fees_bps=Decimal("0.10"),
    )


def _slippage_assumptions() -> SlippageAssumptions:
    return SlippageAssumptions(
        model_id="fixed-bps",
        slippage_bps=Decimal("5"),
        model_description="Fixed slippage used for deterministic fixture contracts.",
    )


def _artifact() -> ReportArtifact:
    return ReportArtifact(
        artifact_id="artifact-1",
        artifact_type="json",
        uri="memory://phase-1a/example.json",
        content_type="application/json",
        sha256="abc123",
        created_at=NOW,
    )


def _run_manifest(parameters: dict[str, str | int | float | bool | None]) -> RunManifest:
    return RunManifest(
        run_id="run-1",
        run_type=RunType.SCAN,
        created_at=NOW,
        code_lineage=CodeLineage(code_version="0.1.0", git_commit="abc123", dirty_tree=True),
        data_snapshot_id="snapshot-1",
        universe_id="fixture-universe",
        symbols=(_symbol(),),
        timeframe=Timeframe.PREMARKET_SNAPSHOT,
        strategy_id=_strategy_id(),
        strategy_version=_strategy_version(),
        parameters=parameters,
        fee_assumptions=_fee_assumptions(),
        slippage_assumptions=_slippage_assumptions(),
        execution_assumptions=_execution_assumptions(),
        source_data=(
            DataLineage(
                data_snapshot_id="snapshot-1",
                source_id=_source_id(),
                source_kind="fixture",
                rows_read=1,
                rows_accepted=1,
                rows_rejected=0,
                source_refs=("sample_data/premarket_snapshot_sample.csv",),
                validation_report_id="validation-1",
            ),
        ),
        output_artifacts=(_artifact(),),
    )


def test_data_ingestion_contracts_can_be_instantiated() -> None:
    batch = BarBatch(
        batch_id="batch-1",
        source_id=_source_id(),
        symbol=_symbol(),
        asset_class=AssetClass.EQUITY,
        timeframe=Timeframe.MINUTE_1,
        bars=(_bar(),),
        created_at=NOW,
    )
    snapshot = DataSnapshot(
        snapshot_id="snapshot-1",
        source_id=_source_id(),
        created_at=NOW,
        as_of=NOW,
        asset_class=AssetClass.EQUITY,
        timeframe=Timeframe.MINUTE_1,
        symbols=(_symbol(),),
        batches=(batch,),
    )
    issue = DataValidationIssue(
        issue_id="issue-1",
        severity=DataValidationSeverity.WARNING,
        code="MISSING_FLOAT",
        message="Float was unavailable in fixture input.",
        symbol=_symbol(),
        field_name="float_shares",
        source_id=_source_id(),
    )
    report = DataValidationReport(
        report_id="validation-1",
        snapshot_id=snapshot.snapshot_id,
        source_id=_source_id(),
        created_at=NOW,
        status=DataValidationStatus.PASSED_WITH_WARNINGS,
        issues=(issue,),
    )

    assert snapshot.batches == (batch,)
    assert report.issues[0].severity is DataValidationSeverity.WARNING


def test_scan_outcome_report_risk_and_decision_contracts_can_be_instantiated() -> None:
    candidate = _scan_candidate()
    scan_result = ScanResult(
        run_id="run-1",
        created_at=NOW,
        strategy_id=_strategy_id(),
        strategy_version=_strategy_version(),
        data_snapshot_id="snapshot-1",
        candidates=(candidate,),
    )
    outcome = TradeOutcome(
        outcome_id="outcome-1",
        candidate_id=candidate.candidate_id,
        symbol=_symbol(),
        label=OutcomeLabel.PENDING,
        opened_at=None,
    )
    metric = PerformanceMetric(
        metric_id="avg-return",
        name="Average return",
        value=Decimal("0.00"),
        unit="pct",
        sample_size=0,
        evidence_status="pending",
    )
    backtest = BacktestSummary(
        summary_id="backtest-summary-1",
        run_id="backtest-1",
        created_at=NOW,
        strategy_id=_strategy_id(),
        strategy_version=_strategy_version(),
        start_at=NOW,
        end_at=NOW,
        trade_count=0,
        metrics=(metric,),
        fees_assumption="zero commission fixture",
        slippage_assumption="5 bps fixture",
    )
    returns = HistoricalReturnSummary(
        summary_id="returns-1",
        created_at=NOW,
        source_run_id="run-1",
        period_start=NOW,
        period_end=NOW,
        selected_count=1,
        outcome_count=0,
        missing_outcome_count=1,
        metrics=(metric,),
    )
    sizing_input = PositionSizingInput(
        request_id="size-request-1",
        symbol=_symbol(),
        account_equity=Decimal("10000"),
        entry_price=Decimal("10.90"),
        stop_price=Decimal("9.75"),
        risk_per_trade_pct=Decimal("0.01"),
        risk_config=_risk_config(),
    )
    trade_plan = TradePlan(
        plan_id="plan-1",
        candidate_id=candidate.candidate_id,
        symbol=_symbol(),
        direction=SignalDirection.LONG,
        entry_price=Decimal("10.90"),
        stop_price=Decimal("9.75"),
        target_prices=(Decimal("12.00"),),
        position_size=_position_result(),
        invalidation="Breaks premarket low",
        created_at=NOW,
    )
    decision = DecisionCard(
        card_id="card-1",
        symbol=_symbol(),
        strategy_id=_strategy_id(),
        strategy_version=_strategy_version(),
        generated_at=NOW,
        signal_status=SignalStatus.WATCHLIST,
        direction=SignalDirection.LONG,
        summary="Fixture decision card for contract validation.",
        evidence=candidate.evidence,
        trade_plan=trade_plan,
        risk_result=_position_result(),
    )

    assert scan_result.candidates == (candidate,)
    assert outcome.label is OutcomeLabel.PENDING
    assert backtest.metrics == (metric,)
    assert returns.missing_outcome_count == 1
    assert sizing_input.risk_config.allow_live_execution is False
    assert decision.research_only is True


def test_required_fields_and_timezone_awareness_are_enforced() -> None:
    with pytest.raises(TypeError):
        Bar(  # type: ignore[call-arg]
            symbol=_symbol(),
            timeframe=Timeframe.MINUTE_1,
            timestamp=NOW,
            open_price=Decimal("1"),
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            volume=1,
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(
            symbol=_symbol(),
            timeframe=Timeframe.MINUTE_1,
            timestamp=datetime(2026, 6, 27, 14, 30),
            open_price=Decimal("1"),
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            volume=1,
            source_id=_source_id(),
        )


def test_invalid_enum_values_fail() -> None:
    with pytest.raises(TypeError):
        ScanCandidate(
            candidate_id="candidate-invalid",
            symbol=_symbol(),
            direction="sideways",  # type: ignore[arg-type]
            status=SignalStatus.WATCHLIST,
            strategy_id=_strategy_id(),
            strategy_version=_strategy_version(),
            setup_score=SetupScore(total=Decimal("1"), grade="C", components=()),
            evidence=(),
            generated_at=NOW,
        )

    payload = _scan_candidate().to_dict()
    payload["direction"] = "sideways"
    with pytest.raises(ValueError):
        ScanCandidate.from_dict(payload)


def test_json_serialization_is_deterministic_and_round_trips() -> None:
    manifest_a = _run_manifest({"min_volume": 100000, "include_fixture": True, "note": "sample"})
    manifest_b = _run_manifest({"note": "sample", "include_fixture": True, "min_volume": 100000})

    json_a = manifest_a.to_json()
    json_b = manifest_b.to_json()

    assert json_a == json_b
    assert '"run_type":"scan"' in json_a
    assert '"slippage_bps":"5"' in json_a
    assert RunManifest.from_json(json_a) == manifest_a


def test_audit_manifests_preserve_key_lineage_fields() -> None:
    run_manifest = _run_manifest({"min_volume": 100000})
    report_manifest = ReportManifest(
        report_id="report-1",
        report_type=ReportType.SCAN_SUMMARY,
        created_at=NOW,
        source_run_id=run_manifest.run_id,
        source_data_snapshot_id=run_manifest.data_snapshot_id,
        generated_artifacts=(_artifact(),),
        execution_assumptions=_execution_assumptions(),
        fee_assumptions=_fee_assumptions(),
        slippage_assumptions=_slippage_assumptions(),
        warnings=("fixture only",),
    )

    decoded = ReportManifest.from_json(report_manifest.to_json())

    assert decoded.source_run_id == "run-1"
    assert decoded.source_data_snapshot_id == "snapshot-1"
    assert decoded.generated_artifacts[0].artifact_id == "artifact-1"
    assert decoded.execution_assumptions.research_only is True


def test_v2_contract_modules_have_no_ui_database_or_network_imports() -> None:
    forbidden_import_roots = {
        "app",
        "sqlite3",
        "streamlit",
        "socket",
        "urllib",
        "requests",
        "httpx",
    }
    forbidden_calls = {"connect", "urlopen", "request"}

    for path in Path("intraday_scanner/v2").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path
