"""Safe runnable v2 Alpha Lab vertical slice."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from intraday_scanner.public_data.yahoo_chart_fetcher import fetch_yahoo_chart_daily_dataset
from intraday_scanner.v2.alpha_lab.robustness import write_robustness_artifacts
from intraday_scanner.v2.audit import (
    CodeLineage,
    DataLineage,
    ExecutionAssumptions,
    FeeAssumptions,
    RunManifest,
    RunType,
    SlippageAssumptions,
)
from intraday_scanner.v2.backtest import BacktestEngine, BacktestResult, BacktestSettings
from intraday_scanner.v2.contracts import (
    DataSourceId,
    ReportArtifact,
    StrategyId,
    StrategyVersion,
    Symbol,
)
from intraday_scanner.v2.contracts.data import AssetClass, Timeframe
from intraday_scanner.v2.data import (
    MarketDataset,
    build_synthetic_ohlcv_dataset,
    dataset_to_snapshot,
    discover_ohlcv_csvs,
    filter_incomplete_daily_bars,
    has_minimum_history,
    load_ohlcv_csv,
    validate_dataset,
    write_ohlcv_csv,
)
from intraday_scanner.v2.paper import (
    PaperLifecycleResult,
    PaperLifecycleSettings,
    run_paper_lifecycle,
    write_paper_artifacts,
)
from intraday_scanner.v2.reports import (
    AlphaLabPaths,
    build_comparison_rows,
    write_alpha_lab_summary,
    write_backtest_artifacts,
    write_json,
    write_strategy_comparison,
)
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.scanner import ScanOutput, run_latest_scan
from intraday_scanner.v2.strategies import build_strategy_catalog
from intraday_scanner.v2.strategies.catalog import describe_strategy


@dataclass(frozen=True)
class AlphaLabRunResult:
    run_id: str
    output_root: Path
    dataset: MarketDataset
    backtest_results: dict[str, BacktestResult]
    scan: ScanOutput
    paper_lifecycle: PaperLifecycleResult
    quality_ready: bool
    warnings: tuple[str, ...]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Alpha Lab")
    parser.add_argument("command", choices=("demo",), help="Run the safe v2 Alpha Lab demo.")
    parser.add_argument(
        "--output-root",
        default="data/v2_alpha_lab",
        help="Artifact output root. Default: data/v2_alpha_lab",
    )
    parser.add_argument(
        "--offline-fixture",
        action="store_true",
        help="Skip public data fetch and use local/synthetic fixture fallback.",
    )
    args = parser.parse_args(argv)
    if args.command == "demo":
        result = run_demo(
            output_root=Path(args.output_root),
            allow_public_data=not args.offline_fixture,
        )
        _print_summary(result)
        return 0
    return 2


def run_demo(
    *,
    output_root: Path = Path("data/v2_alpha_lab"),
    created_at: datetime | None = None,
    allow_public_data: bool = True,
) -> AlphaLabRunResult:
    now = created_at or datetime.now(timezone.utc)
    paths = AlphaLabPaths.create(output_root)
    run_id = f"alpha_lab_demo_{now.strftime('%Y%m%dT%H%M%SZ')}"
    dataset, data_warnings = _load_demo_dataset(paths, now=now, allow_public_data=allow_public_data)
    validation = validate_dataset(
        dataset,
        min_bars_per_symbol=120,
        max_staleness_days=10,
        as_of=now,
    )
    snapshot, validation_report = dataset_to_snapshot(dataset, validation, created_at=now)
    write_json(paths.fixtures / "data_snapshot.json", snapshot.to_dict())
    write_json(paths.fixtures / "data_validation_report.json", validation_report.to_dict())
    write_json(
        paths.reports / "strategy_catalog.json",
        [describe_strategy(strategy) for strategy in build_strategy_catalog()],
    )

    settings = BacktestSettings(
        initial_capital=100_000.0,
        fee_bps=1.0,
        slippage_bps=5.0,
        commission_per_trade=0.0,
        risk=RiskSettings(account_equity=100_000.0, risk_per_trade_pct=0.01, max_position_pct=0.20),
    )
    engine = BacktestEngine(settings=settings)
    strategies = build_strategy_catalog()
    results: dict[str, BacktestResult] = {}
    artifact_paths: list[Path] = [
        paths.fixtures / "data_snapshot.json",
        paths.fixtures / "data_validation_report.json",
        paths.reports / "strategy_catalog.json",
        *_local_source_ref_paths(dataset),
    ]
    for strategy in strategies:
        result = engine.run(strategy, dataset)
        results[strategy.strategy_id] = result
        artifact_paths.extend(write_backtest_artifacts(paths, result).values())

    comparison_csv, comparison_json, comparison_rows = write_strategy_comparison(paths, results)
    artifact_paths.extend([comparison_csv, comparison_json])
    artifact_paths.extend(
        write_robustness_artifacts(
            paths,
            dataset=dataset,
            strategies=strategies,
            settings=settings,
            baseline_results=results,
        ).values()
    )
    scan_strategies = tuple(
        strategy for strategy in strategies if strategy.status not in {"baseline", "benchmark"}
    )
    scan = run_latest_scan(
        dataset,
        scan_strategies,
        results,
        risk_settings=settings.risk,
        data_snapshot_id=snapshot.snapshot_id,
        run_manifest_id=run_id,
    )
    latest_scan_path = paths.scans / "latest_scan.json"
    decision_cards_path = paths.scans / "latest_decision_cards.json"
    write_json(latest_scan_path, scan.to_dict())
    write_json(decision_cards_path, [card.to_dict() for card in scan.cards])
    artifact_paths.extend([latest_scan_path, decision_cards_path])

    paper_lifecycle = run_paper_lifecycle(
        dataset,
        validation,
        scan_strategies,
        run_id=run_id,
        data_snapshot_id=snapshot.snapshot_id,
        settings=PaperLifecycleSettings(account_equity=settings.initial_capital),
    )
    artifact_paths.extend(write_paper_artifacts(paths, paper_lifecycle).values())

    summary_path = write_alpha_lab_summary(
        paths,
        dataset=dataset,
        comparison_rows=comparison_rows,
        scan=scan,
        paper_lifecycle=paper_lifecycle,
        run_id=run_id,
        assumptions={
            "initial_capital": settings.initial_capital,
            "fee_bps": settings.fee_bps,
            "slippage_bps": settings.slippage_bps,
            "commission_per_trade": settings.commission_per_trade,
        },
    )
    artifact_paths.append(summary_path)

    manifest_artifacts = _copy_artifacts_for_manifest(paths.root, run_id, artifact_paths)
    manifest = _build_manifest(
        run_id=run_id,
        created_at=now,
        dataset=dataset,
        snapshot_id=snapshot.snapshot_id,
        validation_report_id=validation_report.report_id,
        artifacts=manifest_artifacts,
        output_root=paths.root,
        settings=settings,
        warnings=tuple(data_warnings + list(validation.warnings) + list(scan.warnings)),
    )
    manifest_path = paths.manifests / f"{run_id}.json"
    write_json(manifest_path, manifest.to_dict())

    return AlphaLabRunResult(
        run_id=run_id,
        output_root=paths.root,
        dataset=dataset,
        backtest_results=results,
        scan=scan,
        paper_lifecycle=paper_lifecycle,
        quality_ready=validation.passed,
        warnings=tuple(
            dict.fromkeys(
                data_warnings
                + list(validation.warnings)
                + list(scan.warnings)
                + list(paper_lifecycle.warnings)
            )
        ),
    )


def _load_demo_dataset(
    paths: AlphaLabPaths,
    *,
    now: datetime,
    allow_public_data: bool,
) -> tuple[MarketDataset, list[str]]:
    warnings: list[str] = []
    if allow_public_data:
        fetch_result = fetch_yahoo_chart_daily_dataset(cache_dir=paths.fixtures / "public_yahoo")
        warnings.extend(fetch_result.warnings)
        public_dataset = filter_incomplete_daily_bars(fetch_result.dataset, as_of=now)
        validation = validate_dataset(
            public_dataset,
            min_bars_per_symbol=120,
            max_staleness_days=10,
            as_of=now,
        )
        if validation.passed and has_minimum_history(
            public_dataset, min_bars_per_symbol=120, min_symbols=3
        ):
            warnings.append("using public Yahoo Finance chart daily OHLCV cache")
            warnings.extend(validation.warnings)
            return public_dataset, warnings
        warnings.extend(validation.issues)
        warnings.extend(validation.warnings)
        warnings.append("public Yahoo Finance chart data unavailable or insufficient")
    else:
        warnings.append("public data fetch skipped by offline fixture setting")

    candidates = discover_ohlcv_csvs(Path("sample_data"))
    for path in candidates:
        dataset = load_ohlcv_csv(
            path,
            dataset_id=f"local_fixture_{path.stem}",
            source_kind="fixture",
            timeframe="1d" if "minute" not in path.as_posix().lower() else "intraday_sparse",
        )
        if has_minimum_history(dataset, min_bars_per_symbol=120, min_symbols=3):
            warnings.append(f"using local fixture dataset {path.as_posix()}")
            return dataset, warnings
        warnings.append(
            f"local fixture {path.as_posix()} was discovered but is too sparse "
            "for strategy comparison"
        )

    synthetic = build_synthetic_ohlcv_dataset(end_date=now.date())
    fixture_path = paths.fixtures / "synthetic_ohlcv.csv"
    write_ohlcv_csv(synthetic, fixture_path)
    warnings.append(
        "adequate real/local OHLCV history was unavailable; generated deterministic "
        "synthetic fixture"
    )
    return (
        MarketDataset(
            dataset_id=synthetic.dataset_id,
            source_kind=synthetic.source_kind,
            timeframe=synthetic.timeframe,
            bars_by_symbol=synthetic.bars_by_symbol,
            source_path=str(fixture_path.as_posix()),
            warnings=synthetic.warnings,
        ),
        warnings,
    )


def _build_manifest(
    *,
    run_id: str,
    created_at: datetime,
    dataset: MarketDataset,
    snapshot_id: str,
    validation_report_id: str,
    artifacts: list[Path],
    output_root: Path,
    settings: BacktestSettings,
    warnings: tuple[str, ...],
) -> RunManifest:
    if dataset.source_refs:
        source_refs = dataset.source_refs
    elif dataset.source_path:
        source_refs = (dataset.source_path,)
    else:
        source_refs = ("generated:synthetic_fixture",)
    rows_rejected = _excluded_bar_count(warnings)
    rows_read = dataset.total_bars + rows_rejected
    return RunManifest(
        run_id=run_id,
        run_type=RunType.BACKTEST,
        created_at=created_at,
        code_lineage=CodeLineage(code_version="0.1.0", dirty_tree=True),
        data_snapshot_id=snapshot_id,
        universe_id=f"{dataset.dataset_id}:universe",
        symbols=tuple(Symbol(symbol, AssetClass.EQUITY) for symbol in dataset.symbols),
        timeframe=Timeframe.DAILY,
        strategy_id=StrategyId("alpha_lab_bundle"),
        strategy_version=StrategyVersion("v1.0"),
        parameters={
            "initial_capital": settings.initial_capital,
            "fee_bps": settings.fee_bps,
            "slippage_bps": settings.slippage_bps,
            "risk_per_trade_pct": settings.risk.risk_per_trade_pct,
        },
        fee_assumptions=FeeAssumptions(
            model_id="fixed_bps_fee",
            commission_per_trade=_decimal_string(settings.commission_per_trade),
            regulatory_fees_bps=_decimal_string(settings.fee_bps),
            notes="Applied as bps fee per fill in the v2 Alpha Lab backtester.",
        ),
        slippage_assumptions=SlippageAssumptions(
            model_id="fixed_bps_slippage",
            slippage_bps=_decimal_string(settings.slippage_bps),
            model_description="Fixed bps slippage applied against entry and exit fills.",
        ),
        execution_assumptions=ExecutionAssumptions(
            assumption_id="alpha_lab_research_only_next_bar",
            research_only=True,
            order_type="none",
            fill_model="next_bar_open_with_ohlc_stop_first",
            allow_live_execution=False,
        ),
        source_data=(
            DataLineage(
                data_snapshot_id=snapshot_id,
                source_id=DataSourceId(dataset.dataset_id),
                source_kind=dataset.source_kind,
                rows_read=rows_read,
                rows_accepted=dataset.total_bars,
                rows_rejected=rows_rejected,
                source_refs=source_refs,
                validation_report_id=validation_report_id,
            ),
        ),
        output_artifacts=tuple(
            _artifact_for_path(path, output_root, created_at) for path in artifacts
        ),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _excluded_bar_count(warnings: tuple[str, ...]) -> int:
    prefix = "excluded "
    total = 0
    for warning in warnings:
        if not warning.startswith(prefix):
            continue
        count_text = warning[len(prefix) :].split(" ", 1)[0]
        try:
            total += int(count_text)
        except ValueError:
            continue
    return total


def _local_source_ref_paths(dataset: MarketDataset) -> list[Path]:
    refs = [dataset.source_path] if dataset.source_path else []
    refs.extend(dataset.source_refs)
    paths: list[Path] = []
    for ref in refs:
        if not ref or "://" in ref:
            continue
        path = Path(ref)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths), key=lambda item: item.as_posix())


def _copy_artifacts_for_manifest(root: Path, run_id: str, artifacts: list[Path]) -> list[Path]:
    run_root = root / "runs" / run_id
    copied: list[Path] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if not artifact.exists() or not artifact.is_file():
            continue
        try:
            relative = artifact.relative_to(root)
        except ValueError:
            relative = Path("external_sources") / artifact.name
        destination = run_root / relative
        key = destination.as_posix()
        if key in seen:
            continue
        seen.add(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if artifact.resolve() != destination.resolve():
            shutil.copy2(artifact, destination)
        copied.append(destination)
    return copied


def _artifact_for_path(path: Path, root: Path, created_at: datetime) -> ReportArtifact:
    uri = _relative_uri(path, root)
    return ReportArtifact(
        artifact_id=uri.replace("/", ":"),
        artifact_type=path.suffix.lstrip(".") or "file",
        uri=uri,
        content_type=_content_type(path),
        sha256=_sha256(path),
        created_at=created_at,
    )


def _relative_uri(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".csv":
        return "text/csv"
    if path.suffix == ".md":
        return "text/markdown"
    return "application/octet-stream"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal_string(value: float) -> Decimal:
    return Decimal(str(value))


def _print_summary(result: AlphaLabRunResult) -> None:
    comparison_rows = build_comparison_rows(result.backtest_results)
    best = (
        comparison_rows[0] if comparison_rows else {"strategy_id": "n/a", "total_return_pct": 0.0}
    )
    print(f"Dawnstrike v2 Alpha Lab demo complete: {result.run_id}")
    print(f"Output: {result.output_root.as_posix()}")
    print(
        f"Data: {result.dataset.dataset_id} ({result.dataset.source_kind}), "
        f"bars={result.dataset.total_bars}"
    )
    print(f"Strategies tested: {len(result.backtest_results)}")
    print(
        "Paper lifecycle: "
        f"{len(result.paper_lifecycle.picks)} picks, "
        f"{len(result.paper_lifecycle.entries)} entries, "
        f"{len(result.paper_lifecycle.exits)} exits"
    )
    print(
        "Best historical performer: "
        f"{best['strategy_id']} ({_as_float(best['total_return_pct']) * 100:.2f}% total return)"
    )
    print(f"Latest scan candidates: {len(result.scan.cards)}")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings[:6]:
            print(f"- {warning}")


def _as_float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"expected numeric value, got {type(value).__name__}")
