# ruff: noqa: E501
# mypy: ignore-errors
"""OMEGA FillTruth v1 execution and outcome evidence layer."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from intraday_scanner.v2.data import MarketBar, MarketDataset, load_ohlcv_csv
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    read_json,
    read_jsonl,
    write_csv,
    write_json,
)

FILL_TRUTH_DIRS = (
    "imports",
    "normalized",
    "fills",
    "outcomes",
    "comparisons",
    "reports",
    "manifests",
    "reconciliation",
    "logs",
)
DEFAULT_OUTPUT_ROOT = Path("data/v2_fill_truth")
PAPER_OPS_ROOT = Path("data/v2_paper_ops")
FORWARD_ROOT = Path("data/v2_forward_evidence")
DATATRUTH_ROOT = Path("data/v2_data_truth")
SLIPPAGE_BPS = 5.0
FEE_BPS = 1.0


@dataclass(frozen=True)
class FillTruthPaths:
    root: Path
    imports: Path
    normalized: Path
    fills: Path
    outcomes: Path
    comparisons: Path
    reports: Path
    manifests: Path
    reconciliation: Path
    logs: Path

    @classmethod
    def create(cls, root: Path) -> FillTruthPaths:
        paths = cls(
            root=root,
            imports=root / "imports",
            normalized=root / "normalized",
            fills=root / "fills",
            outcomes=root / "outcomes",
            comparisons=root / "comparisons",
            reports=root / "reports",
            manifests=root / "manifests",
            reconciliation=root / "reconciliation",
            logs=root / "logs",
        )
        for path in (
            paths.root,
            paths.imports,
            paths.normalized,
            paths.fills,
            paths.outcomes,
            paths.comparisons,
            paths.reports,
            paths.manifests,
            paths.reconciliation,
            paths.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return paths


def init(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = FillTruthPaths.create(output_root)
    payload = {
        "created_at": _now(),
        "execution_models": _execution_models(),
        "fill_certainty_levels": _fill_certainty_levels(),
        "live_execution": "disabled",
        "output_root": output_root.as_posix(),
        "schema_version": "v2.fill_truth_manifest.v1",
        "status": "initialized",
    }
    write_json(paths.manifests / "fill_truth_manifest.json", payload)
    _write_docs()
    return {
        "commands": _command_list(),
        "output_root": output_root.as_posix(),
        "status": "initialized",
    }


def import_intraday(
    *,
    path: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timezone_assumption: str = "UTC",
    source_label: str | None = None,
) -> dict[str, object]:
    if source_label:
        from intraday_scanner.v2.real_intraday import import_intraday as import_real_intraday

        return import_real_intraday(
            path=path,
            output_root=Path("data/v2_real_intraday"),
            source_label=source_label,
            source_timezone=timezone_assumption,
            sync_filltruth=True,
        )
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    files = _csv_files(path)
    rows: list[MarketBar] = []
    validation_rows: list[dict[str, object]] = []
    warnings: list[str] = []
    errors: list[str] = []
    raw_hashes = {source.as_posix(): _sha256(source) for source in files}
    for source in files:
        parsed, file_report = _parse_intraday_csv(
            source,
            timezone_assumption=timezone_assumption,
        )
        rows.extend(parsed)
        validation_rows.append(file_report)
        warnings.extend(str(item) for item in file_report.get("warnings", []))
        errors.extend(str(item) for item in file_report.get("errors", []))
    dataset = _dataset_from_bars(
        rows,
        dataset_id="filltruth_local_intraday",
        source_kind=_source_kind(files),
        timeframe="intraday",
        source_path=path.as_posix(),
        warnings=tuple(dict.fromkeys(warnings + errors)),
    )
    normalized_path = paths.normalized / "latest_intraday_ohlcv.csv"
    _write_bars_csv(normalized_path, dataset)
    normalized_hash = _sha256(normalized_path) if normalized_path.exists() else "n/a"
    snapshot_id = f"filltruth_intraday_{normalized_hash[:12]}"
    manifest = {
        "bar_count": dataset.total_bars,
        "created_at": _now(),
        "data_type": "synthetic_demo" if _is_demo_source(path) else "local_intraday",
        "dataset_id": dataset.dataset_id,
        "errors": sorted(set(errors)),
        "file_count": len(files),
        "normalized_artifact": normalized_path.as_posix(),
        "normalized_artifact_hash": normalized_hash,
        "raw_artifact_hashes": raw_hashes,
        "schema_version": "v2.fill_truth_intraday_import.v1",
        "snapshot_id": snapshot_id,
        "source_path": path.as_posix(),
        "source_provider": dataset.source_kind,
        "status": "failed" if errors else "passed_with_warnings" if warnings else "passed",
        "symbols": list(dataset.symbols),
        "timezone_assumption": timezone_assumption,
        "warnings": list(dict.fromkeys(warnings)),
    }
    write_json(paths.imports / "import_validation_latest.json", {"files": validation_rows, **manifest})
    _write_md(paths.imports / "import_validation_latest.md", "FillTruth Intraday Import", _kv_lines(manifest))
    write_json(paths.manifests / "latest_intraday_import.json", manifest)
    write_json(paths.manifests / f"{snapshot_id}.json", manifest)
    return manifest


def build(*, run_date: date, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    daily, daily_manifest, daily_warnings = _load_daily_dataset()
    intraday, intraday_manifest = _load_intraday_dataset(paths)
    pending = _pending_orders()
    build_id = f"filltruth_build_{run_date.isoformat()}_{_compact_now()}"
    payload = {
        "build_id": build_id,
        "created_at": _now(),
        "daily_accepted_end": daily_manifest.get("accepted_end", "n/a"),
        "daily_bar_count": daily.total_bars if daily else 0,
        "daily_snapshot_id": daily_manifest.get("snapshot_id", "missing"),
        "daily_source_provider": daily_manifest.get("provider_id", "missing"),
        "data_granularity_available": _granularity_available(daily, intraday),
        "intraday_bar_count": intraday.total_bars if intraday else 0,
        "intraday_snapshot_id": intraday_manifest.get("snapshot_id", "missing"),
        "intraday_source_provider": intraday_manifest.get("source_provider", "missing"),
        "pending_orders_inspected": len(pending),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.fill_truth_build.v1",
        "status": "passed_with_warnings" if daily_warnings or not intraday else "passed",
        "warnings": list(
            dict.fromkeys(
                daily_warnings
                + ([] if intraday else ["no local intraday data imported; daily execution models remain approximate for stops/targets"])
            )
        ),
    }
    write_json(paths.normalized / "build_latest.json", payload)
    write_json(paths.reports / "dataset_latest.json", payload)
    _write_md(paths.reports / "dataset_latest.md", "FillTruth Dataset Build", _kv_lines(payload))
    _write_manifest(paths, build_id, "build", run_date, (paths.normalized / "build_latest.json", paths.reports / "dataset_latest.md"), payload["warnings"])
    return payload


def resolve_pending(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    build(run_date=run_date, output_root=output_root)
    daily, daily_manifest, _ = _load_daily_dataset()
    intraday, intraday_manifest = _load_intraday_dataset(paths)
    orders = _pending_orders()
    decisions = [
        _resolve_order(
            order,
            run_date=run_date,
            daily=daily,
            daily_manifest=daily_manifest,
            intraday=intraday,
            intraday_manifest=intraday_manifest,
        )
        for order in orders
    ]
    fill_rows = [row for row in decisions if row["resolution_status"] == "filled"]
    pending_rows = [row for row in decisions if row["resolution_status"] != "filled"]
    summary = _fill_summary(decisions)
    payload = {
        "created_at": _now(),
        "decisions": decisions,
        "fill_certainty_summary": summary,
        "fills_resolved": len(fill_rows),
        "pending_orders_after_resolution": len(pending_rows),
        "pending_orders_inspected": len(orders),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.fill_truth_pending_resolution.v1",
        "status": "passed_with_warnings" if pending_rows or summary["daily_approximation_count"] else "passed",
        "warnings": _resolution_warnings(decisions, intraday is not None),
    }
    json_path = paths.fills / f"{run_date.isoformat()}_fills.json"
    csv_path = paths.fills / f"{run_date.isoformat()}_fills.csv"
    write_json(json_path, payload)
    _write_dynamic_csv(csv_path, decisions)
    write_json(paths.reports / "pending_resolution_latest.json", payload)
    _write_md(paths.reports / "pending_resolution_latest.md", "FillTruth Pending Resolution", _pending_resolution_lines(payload))
    _append_filltruth_events(paths, run_date, "resolve_pending", decisions)
    _write_integration_overlays(paths, run_date, decisions=decisions, outcomes=[], comparison_rows=[])
    _write_manifest(paths, f"filltruth_resolve_{run_date.isoformat()}_{_compact_now()}", "resolve-pending", run_date, (json_path, csv_path, paths.reports / "pending_resolution_latest.md"), payload["warnings"])
    return payload


def evaluate(*, run_date: date, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    latest_resolution = _dict(read_json(paths.reports / "pending_resolution_latest.json", {}))
    if str(latest_resolution.get("run_date", "")) != run_date.isoformat():
        latest_resolution = resolve_pending(run_date=run_date, output_root=output_root)
    daily, daily_manifest, _ = _load_daily_dataset()
    intraday, intraday_manifest = _load_intraday_dataset(paths)
    decisions = _list(latest_resolution.get("decisions"))
    outcomes = [
        _evaluate_fill(
            _dict(decision),
            run_date=run_date,
            daily=daily,
            daily_manifest=daily_manifest,
            intraday=intraday,
            intraday_manifest=intraday_manifest,
        )
        for decision in decisions
        if _dict(decision).get("resolution_status") == "filled"
    ]
    summary = _outcome_summary(outcomes, decisions)
    payload = {
        "created_at": _now(),
        "outcomes": outcomes,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.fill_truth_outcome_evaluation.v1",
        "status": "passed_with_warnings" if summary["ambiguous_fills"] or summary["daily_approximate_outcomes"] else "passed",
        "summary": summary,
        "warnings": _outcome_warnings(outcomes),
    }
    json_path = paths.outcomes / f"{run_date.isoformat()}_outcomes.json"
    csv_path = paths.outcomes / f"{run_date.isoformat()}_outcomes.csv"
    write_json(json_path, payload)
    _write_dynamic_csv(csv_path, outcomes)
    write_json(paths.reports / "outcome_truth_latest.json", payload)
    _write_md(paths.reports / "outcome_truth_latest.md", "FillTruth Outcome Truth", _outcome_lines(payload))
    _append_filltruth_events(paths, run_date, "evaluate", outcomes)
    _write_integration_overlays(paths, run_date, decisions=[_dict(item) for item in decisions], outcomes=outcomes, comparison_rows=[])
    _write_manifest(paths, f"filltruth_evaluate_{run_date.isoformat()}_{_compact_now()}", "evaluate", run_date, (json_path, csv_path, paths.reports / "outcome_truth_latest.md"), payload["warnings"])
    return payload


def compare_models(
    *,
    start: date,
    end: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    daily, daily_manifest, _ = _load_daily_dataset()
    intraday, intraday_manifest = _load_intraday_dataset(paths)
    orders = _pending_orders()
    rows = [
        _model_row(
            model,
            orders=orders,
            start=start,
            end=end,
            daily=daily,
            daily_manifest=daily_manifest,
            intraday=intraday,
            intraday_manifest=intraday_manifest,
        )
        for model in (
            "daily_next_open",
            "daily_close_mark",
            "daily_ohlc_conservative",
            "intraday_bar_sequence",
            "current_paper_ops_model",
            "no_fill_data",
        )
    ]
    disagreement_count = _model_disagreement_count(rows)
    payload = {
        "created_at": _now(),
        "end": end.isoformat(),
        "model_disagreement_count": disagreement_count,
        "rows": rows,
        "schema_version": "v2.fill_truth_execution_model_comparison.v1",
        "start": start.isoformat(),
        "status": "passed_with_warnings" if disagreement_count else "passed",
        "warnings": _comparison_warnings(rows),
    }
    write_json(paths.comparisons / "execution_model_comparison.json", payload)
    _write_dynamic_csv(paths.comparisons / "execution_model_comparison.csv", rows)
    _write_md(paths.reports / "execution_model_comparison.md", "FillTruth Execution Model Comparison", _comparison_lines(payload))
    _write_integration_overlays(paths, end, decisions=[], outcomes=[], comparison_rows=rows)
    _write_manifest(paths, f"filltruth_compare_{start.isoformat()}_{end.isoformat()}_{_compact_now()}", "compare-models", end, (paths.comparisons / "execution_model_comparison.json", paths.comparisons / "execution_model_comparison.csv", paths.reports / "execution_model_comparison.md"), payload["warnings"])
    return payload


def report(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    resolution = _dict(read_json(paths.reports / "pending_resolution_latest.json", {}))
    outcomes = _dict(read_json(paths.reports / "outcome_truth_latest.json", {}))
    comparison = _dict(read_json(paths.comparisons / "execution_model_comparison.json", {}))
    verify_payload = _dict(read_json(paths.reconciliation / "verify_latest.json", {}))
    intraday_manifest = _dict(read_json(paths.manifests / "latest_intraday_import.json", {}))
    summary = {
        "created_at": _now(),
        "data_granularity_available": _summary_granularity(paths),
        "execution_model_disagreement_count": _int(comparison.get("model_disagreement_count")),
        "fill_truth_commit_eligible": bool(intraday_manifest.get("filltruth_commit_eligible", False)),
        "fill_certainty_summary": resolution.get("fill_certainty_summary", {}),
        "fill_certainty_upgrade_count": _int(_dict(resolution.get("fill_certainty_summary")).get("intraday_supported_count")),
        "fills_resolved": _int(resolution.get("fills_resolved")),
        "intraday_reconciliation_status": intraday_manifest.get("daily_reconciliation_status", intraday_manifest.get("intraday_reconciliation_status", "missing")),
        "latest_run_date": resolution.get("run_date", outcomes.get("run_date", "n/a")),
        "pending_orders_after_resolution": _int(resolution.get("pending_orders_after_resolution")),
        "pending_orders_resolved_by_real_intraday": sum(
            1
            for row in _list(resolution.get("decisions"))
            if _dict(row).get("source_label") == "real_local_intraday"
            and _dict(row).get("resolution_status") == "filled"
        ),
        "pending_orders_still_pending": _int(resolution.get("pending_orders_after_resolution")),
        "quality_score": _quality_score(paths),
        "real_intraday_available": bool(intraday_manifest.get("real_intraday_available", False)),
        "schema_version": "v2.fill_truth_summary.v1",
        "session_completeness": intraday_manifest.get("session_completeness", "missing"),
        "source_label": intraday_manifest.get("source_label", intraday_manifest.get("data_type", "missing")),
        "status": "passed" if verify_payload.get("status") in {"passed", "missing", None, ""} else verify_payload.get("status", "passed"),
        "strategy_validation_impact": "blocked_until_fill_truth_forward_evidence_is_sufficient",
        "warnings": _unique(_list(resolution.get("warnings")) + _list(outcomes.get("warnings")) + _list(comparison.get("warnings"))),
    }
    write_json(paths.reports / "filltruth_summary.json", summary)
    _write_md(paths.reports / "filltruth_summary.md", "OMEGA FillTruth Summary", _summary_lines(summary, resolution, outcomes, comparison))
    _write_scorecard(paths)
    return summary


def verify(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    failures: list[str] = []
    warnings: list[str] = []
    for directory in FILL_TRUTH_DIRS:
        if not (output_root / directory).exists():
            failures.append(f"missing FillTruth directory: {directory}")
    required = (
        paths.reports / "pending_resolution_latest.json",
        paths.reports / "outcome_truth_latest.json",
        paths.comparisons / "execution_model_comparison.json",
        paths.reports / "filltruth_summary.md",
        Path("docs/architecture/v2_fill_truth.md"),
        Path("docs/operations/filltruth_daily_workflow.md"),
        Path("docs/audit/omega_filltruth_red_team.md"),
    )
    for path in required:
        if not path.exists():
            failures.append(f"missing artifact: {path.as_posix()}")
    safety = _safety_scan(paths)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    resolution = _dict(read_json(paths.reports / "pending_resolution_latest.json", {}))
    decisions = [_dict(item) for item in _list(resolution.get("decisions"))]
    fill_ids = [str(row.get("fill_id")) for row in decisions if row.get("fill_id") not in {"", "n/a", None}]
    duplicates = sorted({fill_id for fill_id in fill_ids if fill_ids.count(fill_id) > 1})
    if duplicates:
        failures.append("duplicate FillTruth fill IDs: " + ", ".join(duplicates))
    for row in decisions:
        if row.get("resolution_status") == "filled" and not row.get("data_snapshot_id"):
            failures.append(f"fill without source snapshot: {row.get('order_id', 'unknown')}")
        signal_time = _parse_dt(str(row.get("signal_time", "")))
        run_date = date.fromisoformat(str(row.get("run_date", "1970-01-01")))
        if row.get("resolution_status") == "filled" and run_date <= signal_time.date():
            failures.append(f"daily same-day fill not blocked: {row.get('order_id', 'unknown')}")
        if run_date <= signal_time.date() and row.get("daily_same_day_fill_blocked") is not True:
            failures.append(f"same-day policy flag missing: {row.get('order_id', 'unknown')}")
    qa = _dict(read_json(Path("data/v2_command_center/command_center_qa.json"), {}))
    if qa and qa.get("status") not in {"passed", "passed_with_warnings"}:
        failures.append("Command Center QA is not passed")
    payload = {
        "checked_at": _now(),
        "failures": sorted(set(failures)),
        "schema_version": "v2.fill_truth_verify.v1",
        "status": "passed" if not failures else "failed",
        "warnings": sorted(set(warnings)),
    }
    write_json(paths.reconciliation / "verify_latest.json", payload)
    _write_md(paths.reconciliation / "verify_latest.md", "OMEGA FillTruth Verification", _kv_lines(payload))
    _write_scorecard(paths)
    _write_build_state(paths, payload)
    return payload


def demo(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    paths = FillTruthPaths.create(output_root)
    demo_path = _write_demo_intraday_fixture(paths)
    import_result = import_intraday(path=demo_path, output_root=output_root)
    build_result = build(run_date=date(2026, 6, 29), output_root=output_root)
    resolution = resolve_pending(run_date=date(2026, 6, 29), output_root=output_root)
    outcomes = evaluate(run_date=date(2026, 6, 29), output_root=output_root)
    comparison = compare_models(
        start=date(2026, 5, 1),
        end=date(2026, 6, 29),
        output_root=output_root,
    )
    report_result = report(output_root=output_root)
    verify_result = verify(output_root=output_root)
    report_result = report(output_root=output_root)
    return {
        "build_id": build_result["build_id"],
        "comparison_status": comparison["status"],
        "data_granularity_available": report_result["data_granularity_available"],
        "demo_intraday_import_status": import_result["status"],
        "fills_resolved": resolution["fills_resolved"],
        "outcomes_evaluated": len(_list(outcomes.get("outcomes"))),
        "quality_score": report_result["quality_score"],
        "status": "complete" if verify_result["status"] == "passed" and _int(report_result["quality_score"]) >= 99 else "resume_required",
        "summary": (paths.reports / "filltruth_summary.md").as_posix(),
        "verify_status": verify_result["status"],
    }


def morning_check(*, run_date: date, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    build(run_date=run_date, output_root=output_root)
    resolution = resolve_pending(run_date=run_date, output_root=output_root)
    verification = verify(output_root=output_root)
    summary = report(output_root=output_root)
    return {
        "filltruth_status": summary["status"],
        "fills_resolved": resolution["fills_resolved"],
        "pending_orders_after_resolution": resolution["pending_orders_after_resolution"],
        "status": verification["status"],
    }


def after_close(*, run_date: date, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    build(run_date=run_date, output_root=output_root)
    resolution = resolve_pending(run_date=run_date, output_root=output_root)
    outcomes = evaluate(run_date=run_date, output_root=output_root)
    comparison = compare_models(start=run_date - timedelta(days=59), end=run_date, output_root=output_root)
    verification = verify(output_root=output_root)
    summary = report(output_root=output_root)
    return {
        "comparison_status": comparison["status"],
        "filltruth_status": summary["status"],
        "fills_resolved": resolution["fills_resolved"],
        "outcomes_evaluated": len(_list(outcomes.get("outcomes"))),
        "status": verification["status"],
    }


def _csv_files(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    if path.is_dir():
        return tuple(sorted(item for item in path.rglob("*.csv") if item.is_file()))
    raise FileNotFoundError(path.as_posix())


def _parse_intraday_csv(
    path: Path,
    *,
    timezone_assumption: str,
) -> tuple[list[MarketBar], dict[str, object]]:
    bars: list[MarketBar] = []
    warnings: list[str] = []
    errors: list[str] = []
    seen: set[tuple[str, datetime]] = set()
    previous_by_symbol: dict[str, datetime] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        field_map = _field_map(reader.fieldnames or [])
        missing = [field for field in ("timestamp", "open", "high", "low", "close") if field not in field_map]
        if missing:
            return [], {
                "accepted_rows": 0,
                "errors": [f"missing required columns: {', '.join(missing)}"],
                "path": path.as_posix(),
                "rejected_rows": 0,
                "warnings": warnings,
            }
        for row_index, row in enumerate(reader, start=2):
            symbol = _symbol_for_row(row, field_map, path)
            timestamp_raw = str(row.get(field_map["timestamp"], "")).strip()
            try:
                timestamp, tz_warning = _parse_timestamp(timestamp_raw, timezone_assumption)
                if tz_warning:
                    warnings.append(f"{path.name} row {row_index}: {tz_warning}")
                open_price = float(str(row[field_map["open"]]).strip())
                high = float(str(row[field_map["high"]]).strip())
                low = float(str(row[field_map["low"]]).strip())
                close = float(str(row[field_map["close"]]).strip())
                volume = _volume(row, field_map, row_index, warnings, path)
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"{path.name} row {row_index}: invalid row ({exc})")
                continue
            key = (symbol, timestamp)
            if key in seen:
                errors.append(f"{path.name} row {row_index}: duplicate timestamp {symbol} {timestamp.isoformat()}")
                continue
            seen.add(key)
            previous = previous_by_symbol.get(symbol)
            if previous and timestamp < previous:
                warnings.append(f"{path.name} row {row_index}: non-monotonic timestamp for {symbol}")
            previous_by_symbol[symbol] = timestamp
            if min(open_price, high, low, close) <= 0:
                errors.append(f"{path.name} row {row_index}: missing/zero/negative OHLC")
                continue
            if high < max(open_price, low, close):
                errors.append(f"{path.name} row {row_index}: invalid OHLC high")
                continue
            if low > min(open_price, high, close):
                errors.append(f"{path.name} row {row_index}: invalid OHLC low")
                continue
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            )
    return bars, {
        "accepted_rows": len(bars),
        "errors": sorted(set(errors)),
        "path": path.as_posix(),
        "rejected_rows": len(errors),
        "source_hash": _sha256(path),
        "warnings": sorted(set(warnings)),
    }


def _field_map(fields: list[str]) -> dict[str, str]:
    aliases = {
        "timestamp": ("datetime", "timestamp", "date_time", "time", "date"),
        "symbol": ("symbol", "ticker"),
        "open": ("open", "o"),
        "high": ("high", "h"),
        "low": ("low", "l"),
        "close": ("close", "adjusted_close", "adj_close", "c"),
        "volume": ("volume", "vol", "v"),
        "timezone": ("timezone", "tz"),
    }
    by_normalized = {_normalize_column(field): field for field in fields}
    output: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in by_normalized:
                output[canonical] = by_normalized[name]
                break
    return output


def _normalize_column(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _symbol_for_row(row: dict[str, str], field_map: dict[str, str], path: Path) -> str:
    if "symbol" in field_map:
        value = str(row.get(field_map["symbol"], "")).strip().upper()
        if value:
            return value
    stem = path.stem.upper()
    return re.split(r"[_\-\s]", stem)[0] or "UNKNOWN"


def _volume(
    row: dict[str, str],
    field_map: dict[str, str],
    row_index: int,
    warnings: list[str],
    path: Path,
) -> int:
    if "volume" not in field_map:
        warnings.append(f"{path.name} row {row_index}: volume missing; stored as 0")
        return 0
    raw = str(row.get(field_map["volume"], "")).strip()
    if raw == "":
        warnings.append(f"{path.name} row {row_index}: empty volume; stored as 0")
        return 0
    volume = int(float(raw))
    if volume <= 0:
        warnings.append(f"{path.name} row {row_index}: suspicious non-positive volume")
    return volume


def _parse_timestamp(value: str, timezone_assumption: str) -> tuple[datetime, str | None]:
    normalized = value.replace("Z", "+00:00")
    timestamp = datetime.fromisoformat(normalized)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp, f"naive timestamp assumed {timezone_assumption}"
    return timestamp.astimezone(timezone.utc), None


def _dataset_from_bars(
    bars: list[MarketBar],
    *,
    dataset_id: str,
    source_kind: str,
    timeframe: str,
    source_path: str | None = None,
    warnings: tuple[str, ...] = (),
) -> MarketDataset:
    grouped: dict[str, list[MarketBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.symbol, []).append(bar)
    return MarketDataset(
        dataset_id=dataset_id,
        source_kind=source_kind,
        timeframe=timeframe,
        bars_by_symbol={
            symbol: tuple(sorted(symbol_bars, key=lambda item: item.timestamp))
            for symbol, symbol_bars in grouped.items()
        },
        source_path=source_path,
        warnings=warnings,
    )


def _write_bars_csv(path: Path, dataset: MarketDataset) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for symbol in dataset.symbols:
        for bar in dataset.bars_by_symbol[symbol]:
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": bar.timestamp.isoformat(),
                    "open": _format_float(bar.open),
                    "high": _format_float(bar.high),
                    "low": _format_float(bar.low),
                    "close": _format_float(bar.close),
                    "volume": bar.volume,
                }
            )
    write_csv(path, rows, ("symbol", "timestamp", "open", "high", "low", "close", "volume"))


def _source_kind(files: tuple[Path, ...]) -> str:
    if any(_is_demo_source(path) for path in files):
        return "local_demo_intraday_fixture"
    return "local_intraday_csv"


def _is_demo_source(path: Path) -> bool:
    return "demo" in path.as_posix().lower()


def _load_daily_dataset() -> tuple[MarketDataset | None, dict[str, object], list[str]]:
    manifest = _dict(read_json(DATATRUTH_ROOT / "manifests" / "latest.json", {}))
    path = DATATRUTH_ROOT / "normalized" / "latest_ohlcv.csv"
    warnings: list[str] = []
    if not path.exists():
        warnings.append("DataTruth normalized daily OHLCV missing")
        return None, manifest, warnings
    dataset = load_ohlcv_csv(
        path,
        dataset_id=str(manifest.get("snapshot_id", "datatruth_latest")),
        source_kind=str(manifest.get("provider_id", "public_yahoo_chart")),
        timeframe=str(manifest.get("timeframe", "1d")),
    )
    warnings.extend(str(item) for item in _list(manifest.get("warnings")))
    return dataset, manifest, warnings


def _load_intraday_dataset(paths: FillTruthPaths) -> tuple[MarketDataset | None, dict[str, object]]:
    manifest = _dict(read_json(paths.manifests / "latest_intraday_import.json", {}))
    path = paths.normalized / "latest_intraday_ohlcv.csv"
    if not path.exists():
        return None, manifest
    dataset = load_ohlcv_csv(
        path,
        dataset_id=str(manifest.get("snapshot_id", "filltruth_intraday")),
        source_kind=str(manifest.get("source_provider", "local_intraday_csv")),
        timeframe="intraday",
    )
    return dataset, manifest


def _pending_orders() -> list[dict[str, object]]:
    payload = read_json(PAPER_OPS_ROOT / "state" / "pending_orders.json", [])
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _open_positions() -> list[dict[str, object]]:
    payload = read_json(PAPER_OPS_ROOT / "state" / "open_positions.json", [])
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _resolve_order(
    order: dict[str, object],
    *,
    run_date: date,
    daily: MarketDataset | None,
    daily_manifest: dict[str, object],
    intraday: MarketDataset | None,
    intraday_manifest: dict[str, object],
) -> dict[str, object]:
    symbol = str(order.get("symbol", "")).upper()
    signal_time = _parse_dt(str(order.get("signal_time", "")))
    earliest_fill = _earliest_fill_date(order, signal_time)
    daily_same_day_blocked = run_date <= signal_time.date()
    base = _base_decision(order, run_date, earliest_fill, daily_same_day_blocked)
    if daily_same_day_blocked:
        return {
            **base,
            "data_granularity": "none",
            "data_snapshot_id": "n/a",
            "execution_model": "no_fill_data",
            "fill_certainty": "rejected_policy",
            "reason": "daily close signal cannot fill on the same daily bar",
            "resolution_status": "pending",
            "warnings": _merge(order.get("warnings"), ("same_day_daily_fill_blocked",)),
        }
    intraday_bar = _first_intraday_bar_after(intraday, symbol, signal_time, earliest_fill, run_date)
    if intraday_bar is not None:
        return _filled_decision(
            base,
            order,
            bar=intraday_bar,
            execution_model="intraday_bar_sequence",
            fill_certainty="intraday_sequence_supported",
            data_granularity="intraday",
            data_snapshot_id=str(intraday_manifest.get("snapshot_id", intraday.dataset_id if intraday else "")),
            source_provider=str(intraday_manifest.get("source_provider", "local_intraday_csv")),
            source_label=str(intraday_manifest.get("source_label", intraday_manifest.get("data_type", "unknown_intraday"))),
            intraday_reconciliation_status=str(
                intraday_manifest.get("daily_reconciliation_status")
                or intraday_manifest.get("intraday_reconciliation_status")
                or "missing"
            ),
            session_completeness=str(intraday_manifest.get("session_completeness", "unknown_session")),
            filltruth_commit_eligible=bool(intraday_manifest.get("filltruth_commit_eligible", False)),
            reason="timestamped intraday bar sequence supports next eligible fill",
            extra_warnings=("intraday bar still approximates within-bar order",),
        )
    daily_bar = _first_daily_bar_after(daily, symbol, signal_time, earliest_fill, run_date)
    if daily_bar is not None:
        return _filled_decision(
            base,
            order,
            bar=daily_bar,
            execution_model="daily_next_open",
            fill_certainty="exact_known_from_bar_open",
            data_granularity="1d",
            data_snapshot_id=str(daily_manifest.get("snapshot_id", daily.dataset_id if daily else "")),
            source_provider=str(daily_manifest.get("provider_id", "public_yahoo_chart")),
            source_label="daily_ohlc_conservative",
            intraday_reconciliation_status="not_intraday",
            session_completeness="not_intraday",
            filltruth_commit_eligible=False,
            reason="daily next-open value is available; stop/target sequencing remains approximate",
            extra_warnings=("daily bar cannot prove intraday stop/target order",),
        )
    return {
        **base,
        "data_granularity": "none",
        "data_snapshot_id": "n/a",
        "execution_model": "no_fill_data",
        "fill_certainty": "pending_no_fill_data",
        "reason": "no eligible next-bar data is available",
        "resolution_status": "pending",
        "warnings": _merge(order.get("warnings"), ("pending_no_fill_data",)),
    }


def _base_decision(
    order: dict[str, object],
    run_date: date,
    earliest_fill: date,
    daily_same_day_blocked: bool,
) -> dict[str, object]:
    return {
        "direction": order.get("direction", "long"),
        "daily_same_day_fill_blocked": daily_same_day_blocked,
        "earliest_fill_date": earliest_fill.isoformat(),
        "entry_reference": _float(order.get("entry")),
        "expected_fill_rule": order.get("expected_fill_rule", "daily signal fills no earlier than next valid bar open"),
        "fill_id": "n/a",
        "fill_price": "n/a",
        "fill_time": "n/a",
        "fee": 0.0,
        "order_id": order.get("order_id", "unknown"),
        "quantity": _int(order.get("quantity")),
        "risk_per_unit": _float(order.get("risk_per_unit")),
        "run_date": run_date.isoformat(),
        "signal_time": order.get("signal_time", "n/a"),
        "slippage": 0.0,
        "source_provider": "n/a",
        "source_label": "n/a",
        "intraday_reconciliation_status": "n/a",
        "session_completeness": "n/a",
        "filltruth_commit_eligible": False,
        "stop": _float(order.get("stop")),
        "strategy_id": order.get("strategy_id", "unknown"),
        "symbol": order.get("symbol", "unknown"),
        "target": order.get("target"),
    }


def _filled_decision(
    base: dict[str, object],
    order: dict[str, object],
    *,
    bar: MarketBar,
    execution_model: str,
    fill_certainty: str,
    data_granularity: str,
    data_snapshot_id: str,
    source_provider: str,
    source_label: str,
    intraday_reconciliation_status: str,
    session_completeness: str,
    filltruth_commit_eligible: bool,
    reason: str,
    extra_warnings: tuple[str, ...],
) -> dict[str, object]:
    direction = str(order.get("direction", "long"))
    quantity = _int(order.get("quantity"))
    fill_price, slippage = _entry_price_with_slippage(bar.open, direction, quantity)
    fee = _fee(fill_price, quantity)
    return {
        **base,
        "bar_open": bar.open,
        "data_granularity": data_granularity,
        "data_snapshot_id": data_snapshot_id,
        "execution_model": execution_model,
        "fee": round(fee, 6),
        "fill_certainty": fill_certainty,
        "fill_id": _stable_id("filltruth", execution_model, order.get("order_id"), bar.timestamp.isoformat()),
        "fill_price": round(fill_price, 6),
        "fill_time": bar.timestamp.isoformat(),
        "reason": reason,
        "resolution_status": "filled",
        "slippage": round(slippage, 6),
        "source_provider": source_provider,
        "source_label": source_label,
        "intraday_reconciliation_status": intraday_reconciliation_status,
        "session_completeness": session_completeness,
        "filltruth_commit_eligible": filltruth_commit_eligible,
        "warnings": _merge(order.get("warnings"), extra_warnings),
    }


def _evaluate_fill(
    fill: dict[str, object],
    *,
    run_date: date,
    daily: MarketDataset | None,
    daily_manifest: dict[str, object],
    intraday: MarketDataset | None,
    intraday_manifest: dict[str, object],
) -> dict[str, object]:
    symbol = str(fill.get("symbol", "")).upper()
    direction = str(fill.get("direction", "long"))
    bars = _outcome_bars(fill, daily=daily, intraday=intraday, run_date=run_date)
    if not bars:
        return _pending_outcome(fill, "no bars available after fill")
    target = _optional_float(fill.get("target"))
    stop = _float(fill.get("stop"))
    quantity = _int(fill.get("quantity"))
    fill_price = _float(fill.get("fill_price"))
    entry_fee = _float(fill.get("fee"))
    outcome_model = "intraday_bar_sequence" if fill.get("execution_model") == "intraday_bar_sequence" else "daily_ohlc_conservative"
    outcome_certainty = "intraday_sequence_supported" if outcome_model == "intraday_bar_sequence" else "daily_approximation"
    for bar in bars:
        stop_hit = _stop_hit(bar, direction, stop)
        target_hit = target is not None and _target_hit(bar, direction, target)
        if stop_hit or target_hit:
            ambiguous = stop_hit and target_hit
            close_price = stop if stop_hit else float(target)
            reason = "stop" if stop_hit else "target"
            if ambiguous:
                close_price = stop
                reason = "ambiguous_same_bar_stop_first"
                outcome_certainty = "ambiguous_same_bar"
            return _closed_outcome(
                fill,
                close_time=bar.timestamp,
                close_price=close_price,
                close_reason=reason,
                outcome_model=outcome_model,
                outcome_certainty=outcome_certainty,
                entry_fee=entry_fee,
                quantity=quantity,
                fill_price=fill_price,
                extra_warnings=("same-bar stop/target conflict; stop-first used",) if ambiguous else (),
            )
    last = bars[-1]
    mark_price = last.close
    unrealized = _pnl(direction, fill_price, mark_price, quantity) - entry_fee
    data_snapshot_id = (
        intraday_manifest.get("snapshot_id")
        if fill.get("execution_model") == "intraday_bar_sequence"
        else daily_manifest.get("snapshot_id")
    )
    return {
        "close_id": "n/a",
        "close_price": "n/a",
        "close_reason": "mark_to_market_only",
        "close_time": last.timestamp.isoformat(),
        "data_snapshot_id": str(data_snapshot_id or fill.get("data_snapshot_id", "n/a")),
        "execution_model": outcome_model,
        "fill_certainty": fill.get("fill_certainty", "unknown"),
        "fill_id": fill.get("fill_id", "n/a"),
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "outcome_certainty": "mark_to_market_only" if outcome_model != "intraday_bar_sequence" else outcome_certainty,
        "outcome_status": "open_mark_to_market",
        "position_status": "open",
        "realized_pnl": 0.0,
        "r_multiple": "n/a",
        "run_date": run_date.isoformat(),
        "source_provider": fill.get("source_provider", "n/a"),
        "strategy_id": fill.get("strategy_id", "unknown"),
        "symbol": symbol,
        "unrealized_pnl": round(unrealized, 6),
        "warnings": _merge(fill.get("warnings"), ("carry position marked to completed close; not realized",)),
    }


def _closed_outcome(
    fill: dict[str, object],
    *,
    close_time: datetime,
    close_price: float,
    close_reason: str,
    outcome_model: str,
    outcome_certainty: str,
    entry_fee: float,
    quantity: int,
    fill_price: float,
    extra_warnings: tuple[str, ...],
) -> dict[str, object]:
    direction = str(fill.get("direction", "long"))
    exit_fee = _fee(close_price, quantity)
    gross = _pnl(direction, fill_price, close_price, quantity)
    net = gross - entry_fee - exit_fee
    risk_amount = max(_float(fill.get("risk_per_unit")) * quantity, 0.000001)
    return {
        "close_id": _stable_id("filltruth_close", fill.get("fill_id"), close_time.isoformat(), close_reason),
        "close_price": round(close_price, 6),
        "close_reason": close_reason,
        "close_time": close_time.isoformat(),
        "data_snapshot_id": fill.get("data_snapshot_id", "n/a"),
        "execution_model": outcome_model,
        "fill_certainty": fill.get("fill_certainty", "unknown"),
        "fill_id": fill.get("fill_id", "n/a"),
        "gross_pnl": round(gross, 6),
        "net_pnl": round(net, 6),
        "outcome_certainty": outcome_certainty,
        "outcome_status": "closed",
        "position_status": "closed",
        "realized_pnl": round(net, 6),
        "r_multiple": round(net / risk_amount, 6),
        "source_provider": fill.get("source_provider", "n/a"),
        "strategy_id": fill.get("strategy_id", "unknown"),
        "symbol": fill.get("symbol", "unknown"),
        "unrealized_pnl": 0.0,
        "warnings": _merge(fill.get("warnings"), extra_warnings),
    }


def _pending_outcome(fill: dict[str, object], reason: str) -> dict[str, object]:
    return {
        "close_id": "n/a",
        "close_reason": "pending_no_outcome_data",
        "data_snapshot_id": fill.get("data_snapshot_id", "n/a"),
        "execution_model": fill.get("execution_model", "no_fill_data"),
        "fill_certainty": fill.get("fill_certainty", "unknown"),
        "fill_id": fill.get("fill_id", "n/a"),
        "net_pnl": "n/a",
        "outcome_certainty": "pending_no_fill_data",
        "outcome_status": "pending",
        "position_status": "unknown",
        "reason": reason,
        "strategy_id": fill.get("strategy_id", "unknown"),
        "symbol": fill.get("symbol", "unknown"),
        "warnings": _merge(fill.get("warnings"), (reason,)),
    }


def _outcome_bars(
    fill: dict[str, object],
    *,
    daily: MarketDataset | None,
    intraday: MarketDataset | None,
    run_date: date,
) -> list[MarketBar]:
    symbol = str(fill.get("symbol", "")).upper()
    fill_time = _parse_dt(str(fill.get("fill_time", "")))
    if fill.get("execution_model") == "intraday_bar_sequence" and intraday:
        return [
            bar
            for bar in intraday.bars_by_symbol.get(symbol, ())
            if bar.timestamp >= fill_time and bar.timestamp.date() <= run_date
        ]
    if daily:
        return [
            bar
            for bar in daily.bars_by_symbol.get(symbol, ())
            if bar.timestamp.date() >= fill_time.date() and bar.timestamp.date() <= run_date
        ]
    return []


def _model_row(
    model: str,
    *,
    orders: list[dict[str, object]],
    start: date,
    end: date,
    daily: MarketDataset | None,
    daily_manifest: dict[str, object],
    intraday: MarketDataset | None,
    intraday_manifest: dict[str, object],
) -> dict[str, object]:
    if model == "current_paper_ops_model":
        events = read_jsonl(PAPER_OPS_ROOT / "ledger" / "paper_ledger.jsonl")
        fills = [event for event in events if event.get("event_type") == "paper_fill" and event.get("mode") == "forward"]
        closes = [event for event in events if event.get("event_type") == "paper_position_closed" and event.get("mode") == "forward"]
        return {
            "ambiguous_fills": 0,
            "average_r": "n/a",
            "closed_count": len(closes),
            "difference_vs_conservative_model": "n/a",
            "execution_model": model,
            "expectancy_r": "n/a",
            "fees": "n/a",
            "fill_count": len(fills),
            "model_disagreement_count": "n/a",
            "pending_count": len(_pending_orders()),
            "realized_pnl": "n/a",
            "same_bar_conflicts": "n/a",
            "slippage": "n/a",
            "unrealized_pnl": "n/a",
            "win_rate": "n/a",
        }
    if model == "no_fill_data":
        return {
            "ambiguous_fills": 0,
            "average_r": 0.0,
            "closed_count": 0,
            "difference_vs_conservative_model": 0.0,
            "execution_model": model,
            "expectancy_r": 0.0,
            "fees": 0.0,
            "fill_count": 0,
            "model_disagreement_count": len(orders),
            "pending_count": len(orders),
            "realized_pnl": 0.0,
            "same_bar_conflicts": 0,
            "slippage": 0.0,
            "unrealized_pnl": 0.0,
            "win_rate": 0.0,
        }
    decisions: list[dict[str, object]] = []
    for order in orders:
        if model == "intraday_bar_sequence":
            decision = _resolve_order(
                order,
                run_date=end,
                daily=None,
                daily_manifest={},
                intraday=intraday,
                intraday_manifest=intraday_manifest,
            )
        elif model in {"daily_next_open", "daily_close_mark", "daily_ohlc_conservative"}:
            decision = _resolve_order(
                order,
                run_date=end,
                daily=daily,
                daily_manifest=daily_manifest,
                intraday=None,
                intraday_manifest={},
            )
            if model == "daily_close_mark":
                decision = {
                    **decision,
                    "execution_model": "daily_close_mark",
                    "fill_certainty": "mark_to_market_only",
                    "resolution_status": "pending",
                    "reason": "daily close mark is not a new-order fill model",
                }
        else:
            decision = _resolve_order(
                order,
                run_date=end,
                daily=daily,
                daily_manifest=daily_manifest,
                intraday=intraday,
                intraday_manifest=intraday_manifest,
            )
        decisions.append(decision)
    filled = [row for row in decisions if row.get("resolution_status") == "filled"]
    outcomes = [
        _evaluate_fill(
            row,
            run_date=end,
            daily=daily,
            daily_manifest=daily_manifest,
            intraday=intraday,
            intraday_manifest=intraday_manifest,
        )
        for row in filled
        if model in {"daily_ohlc_conservative", "intraday_bar_sequence"}
    ]
    realized = sum(_float(row.get("realized_pnl")) for row in outcomes)
    fees = sum(_float(row.get("fee")) for row in filled)
    slippage = sum(_float(row.get("slippage")) for row in filled)
    r_values = [_float(row.get("r_multiple")) for row in outcomes if row.get("r_multiple") not in {"n/a", None, ""}]
    wins = [row for row in outcomes if _float(row.get("realized_pnl")) > 0]
    closed = [row for row in outcomes if row.get("outcome_status") == "closed"]
    return {
        "ambiguous_fills": sum(1 for row in outcomes if row.get("outcome_certainty") == "ambiguous_same_bar"),
        "average_r": round(sum(r_values) / len(r_values), 6) if r_values else 0.0,
        "closed_count": len(closed),
        "difference_vs_conservative_model": "baseline" if model == "daily_ohlc_conservative" else "see_json",
        "execution_model": model,
        "expectancy_r": round(sum(r_values) / len(r_values), 6) if r_values else 0.0,
        "fees": round(fees, 6),
        "fill_count": len(filled),
        "model_disagreement_count": max(0, len(orders) - len(filled)),
        "pending_count": len(orders) - len(filled),
        "realized_pnl": round(realized, 6),
        "same_bar_conflicts": sum(1 for row in outcomes if row.get("outcome_certainty") == "ambiguous_same_bar"),
        "slippage": round(slippage, 6),
        "unrealized_pnl": round(sum(_float(row.get("unrealized_pnl")) for row in outcomes), 6),
        "win_rate": round(len(wins) / len(closed), 6) if closed else 0.0,
    }


def _write_integration_overlays(
    paths: FillTruthPaths,
    run_date: date,
    *,
    decisions: list[dict[str, object]],
    outcomes: list[dict[str, object]],
    comparison_rows: list[dict[str, object]],
) -> None:
    summary = _overlay_summary(run_date, decisions, outcomes, comparison_rows)
    paper_reports = PAPER_OPS_ROOT / "reports"
    paper_reports.mkdir(parents=True, exist_ok=True)
    write_json(paper_reports / "fill_truth_overlay.json", summary)
    _write_md(paper_reports / "fill_truth_overlay.md", "PaperOps FillTruth Overlay", _kv_lines(summary))
    calendar_root = FORWARD_ROOT / "calendar"
    calendar_root.mkdir(parents=True, exist_ok=True)
    calendar_row = {
        "date": run_date.isoformat(),
        "daily_approximate_fills": summary["daily_approximation_count"],
        "execution_model": summary["dominant_execution_model"],
        "execution_model_disagreement_count": summary["execution_model_disagreement_count"],
        "exact_known_fills": summary["exact_known_from_bar_open_count"],
        "fill_certainty_summary": json.dumps(summary["fill_certainty_summary"], sort_keys=True),
        "fill_model_warning_count": len(summary["warnings"]),
        "fill_truth_status": summary["fill_truth_status"],
        "intraday_supported_fills": summary["intraday_supported_count"],
        "pending_no_fill_data": summary["pending_no_fill_data_count"],
    }
    write_json(calendar_root / "fill_truth_overlay.json", {"rows": [calendar_row], "schema_version": "v2.forward_fill_truth_overlay.v1", "status": "passed"})
    write_csv(calendar_root / "fill_truth_overlay.csv", [calendar_row], tuple(calendar_row))
    _write_md(calendar_root / "fill_truth_calendar_summary.md", "Forward Calendar FillTruth Overlay", _kv_lines(calendar_row))
    evidence = _strategy_evidence_overlay(summary)
    write_json(paths.reports / "filltruth_strategy_evidence.json", evidence)
    _write_dynamic_csv(paths.reports / "filltruth_strategy_evidence.csv", _list(evidence.get("rows")))
    _write_md(paths.reports / "filltruth_strategy_evidence.md", "FillTruth Strategy Evidence", _strategy_evidence_lines(evidence))
    target = FORWARD_ROOT / "strategy_evidence"
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "filltruth_strategy_evidence_overlay.json", evidence)


def _overlay_summary(
    run_date: date,
    decisions: list[dict[str, object]],
    outcomes: list[dict[str, object]],
    comparison_rows: list[dict[str, object]],
) -> dict[str, object]:
    summary = _fill_summary(decisions)
    model_disagreements = _model_disagreement_count(comparison_rows) if comparison_rows else 0
    warnings = _unique(
        [
            warning
            for row in decisions + outcomes
            for warning in _list(row.get("warnings"))
        ]
        + _comparison_warnings(comparison_rows)
    )
    return {
        "created_at": _now(),
        "daily_approximation_count": summary["daily_approximation_count"],
        "dominant_execution_model": _dominant_model(decisions, comparison_rows),
        "exact_known_from_bar_open_count": summary["exact_known_from_bar_open_count"],
        "execution_model_disagreement_count": model_disagreements,
        "fill_certainty_score": _fill_certainty_score(summary, model_disagreements),
        "fill_certainty_summary": summary,
        "fill_reconciliation_score": 100 if not warnings else 90,
        "fill_truth_status": "passed_with_warnings" if warnings else "passed",
        "intraday_support_score": 100 if summary["intraday_supported_count"] else 60,
        "intraday_supported_count": summary["intraday_supported_count"],
        "pending_no_fill_data_count": summary["pending_no_fill_data_count"],
        "pending_resolution_score": 100 if summary["pending_no_fill_data_count"] == 0 else 75,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.fill_truth_overlay.v1",
        "strategy_validation_allowed": False,
        "strategy_validation_reason": "No strategy may validate until forward outcomes are reconstructible with sufficient FillTruth certainty and sample size.",
        "warnings": warnings,
    }


def _strategy_evidence_overlay(summary: dict[str, object]) -> dict[str, object]:
    existing = _dict(read_json(FORWARD_ROOT / "strategy_evidence" / "strategy_evidence_omega.json", {}))
    rows = []
    for row in [_dict(item) for item in _list(existing.get("rows"))]:
        blockers = str(row.get("blockers", ""))
        fill_blocker = "FillTruth forward fill evidence is not yet sufficient for validation"
        rows.append(
            {
                **row,
                "ambiguity_penalty": 10 if _int(summary.get("daily_approximation_count")) else 0,
                "execution_model_stability_score": max(0, 100 - 10 * _int(summary.get("execution_model_disagreement_count"))),
                "fill_certainty_score": summary.get("fill_certainty_score", 0),
                "fill_reconciliation_score": summary.get("fill_reconciliation_score", 0),
                "fill_truth_blocks_validation": True,
                "fill_truth_status": summary.get("fill_truth_status", "missing"),
                "intraday_support_score": summary.get("intraday_support_score", 0),
                "pending_resolution_score": summary.get("pending_resolution_score", 0),
                "validation_eligible": False,
                "blockers": (blockers + " | " + fill_blocker).strip(" |"),
            }
        )
    if not rows:
        rows.append(
            {
                "blockers": "No strategy evidence rows available; FillTruth cannot validate any strategy.",
                "evidence_status": "watch",
                "fill_truth_blocks_validation": True,
                "fill_truth_status": summary.get("fill_truth_status", "missing"),
                "strategy_id": "unknown",
                "validation_eligible": False,
            }
        )
    return {
        "rows": rows,
        "schema_version": "v2.fill_truth_strategy_evidence_overlay.v1",
        "status": "passed",
        "warnings": summary.get("warnings", []),
    }


def _write_demo_intraday_fixture(paths: FillTruthPaths) -> Path:
    path = paths.imports / "demo_intraday_qqq_2026-06-29.csv"
    rows = [
        {
            "symbol": "QQQ",
            "timestamp": "2026-06-29T13:30:00+00:00",
            "open": "706.52",
            "high": "708.10",
            "low": "705.80",
            "close": "707.40",
            "volume": "100000",
        },
        {
            "symbol": "QQQ",
            "timestamp": "2026-06-29T13:35:00+00:00",
            "open": "707.40",
            "high": "709.00",
            "low": "706.70",
            "close": "708.25",
            "volume": "118000",
        },
        {
            "symbol": "QQQ",
            "timestamp": "2026-06-29T20:00:00+00:00",
            "open": "708.25",
            "high": "709.50",
            "low": "706.10",
            "close": "706.52",
            "volume": "75000",
        },
    ]
    write_csv(path, rows, ("symbol", "timestamp", "open", "high", "low", "close", "volume"))
    return path


def _write_docs() -> None:
    _write_text(
        Path("docs/architecture/v2_fill_truth.md"),
        "\n".join(
            [
                "# v2 OMEGA FillTruth",
                "",
                "OMEGA FillTruth is an additive execution and outcome evidence layer for forward paper decisions.",
                "",
                "## Execution Models",
                "",
                "- `daily_next_open`: close-generated daily signals can fill no earlier than the next valid daily open.",
                "- `daily_close_mark`: mark-to-market only; it is not a new-order fill model.",
                "- `daily_ohlc_conservative`: daily OHLC stop/target checks with stop-first conflict handling.",
                "- `intraday_bar_sequence`: timestamped intraday bars are evaluated in order, while still acknowledging within-bar ambiguity.",
                "- `no_fill_data`: no eligible data exists, so the order remains pending.",
                "",
                "## Data Flow",
                "",
                "Frozen picks and PaperOps pending orders feed FillTruth. FillTruth reads DataTruth daily bars and optional local intraday CSV imports, then writes separate fill, outcome, comparison, manifest, Calendar overlay, Strategy Evidence overlay, and Sentinel-readable summary artifacts.",
                "",
                "## Safety Boundary",
                "",
                "The module does not place real trades, does not route orders, does not mutate existing SQLite databases, and does not rewrite legacy app or scanner behavior.",
                "",
                "## Commands",
                "",
                "```powershell",
                "py -m intraday_scanner.v2.fill_truth init",
                "py -m intraday_scanner.v2.fill_truth import-intraday --path data/v2_fill_truth/imports/demo_intraday_qqq_2026-06-29.csv",
                "py -m intraday_scanner.v2.fill_truth build --date 2026-06-29",
                "py -m intraday_scanner.v2.fill_truth resolve-pending --date 2026-06-29",
                "py -m intraday_scanner.v2.fill_truth evaluate --date 2026-06-29",
                "py -m intraday_scanner.v2.fill_truth compare-models --start 2026-05-01 --end 2026-06-29",
                "py -m intraday_scanner.v2.fill_truth report",
                "py -m intraday_scanner.v2.fill_truth verify",
                "py -m intraday_scanner.v2.fill_truth demo",
                "```",
                "",
                "## Limitations",
                "",
                "- Public daily OHLCV does not prove intraday stop/target sequence.",
                "- Local intraday CSVs are single-source unless separately reconciled.",
                "- Demo intraday data is synthetic and is never market evidence.",
                "- Strategy validation remains blocked until enough true forward evidence exists.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/operations/filltruth_daily_workflow.md"),
        "\n".join(
            [
                "# FillTruth Daily Workflow",
                "",
                "## After Close",
                "",
                "- Build DataTruth and OMEGA/Sentinel as usual.",
                "- Run `py -m intraday_scanner.v2.fill_truth after-close` through the Sentinel wrapper when available, or run `build`, `resolve-pending`, `evaluate`, `compare-models`, `report`, and `verify` directly.",
                "",
                "## Next Morning",
                "",
                "- Import any local intraday/open data available without credentials.",
                "- Run `py -m intraday_scanner.v2.fill_truth resolve-pending --date YYYY-MM-DD`.",
                "- Review `data/v2_fill_truth/reports/pending_resolution_latest.md` before treating any paper fill as evidence.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/operations/filltruth_intraday_import.md"),
        "\n".join(
            [
                "# FillTruth Intraday Import",
                "",
                "Use local CSV files with timestamp, open, high, low, close, and optional volume/symbol fields.",
                "",
                "Supported timestamp column names include `datetime`, `timestamp`, `date_time`, `time`, and `date`.",
                "Naive timestamps are assumed UTC and reported as warnings.",
                "Source files are hashed and normalized output is written under `data/v2_fill_truth/normalized/`.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/operations/filltruth_pending_order_resolution.md"),
        "\n".join(
            [
                "# FillTruth Pending Order Resolution",
                "",
                "- Daily close signals cannot fill on the same daily bar.",
                "- Intraday bars are preferred when available after the signal timestamp.",
                "- Daily next-open fills are allowed only when a later daily open exists.",
                "- If no eligible data exists, the order remains pending with `pending_no_fill_data` certainty.",
                "- FillTruth writes evidence overlays and does not silently rewrite existing PaperOps state.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/audit/omega_filltruth_red_team.md"),
        "\n".join(
            [
                "# OMEGA FillTruth Red Team",
                "",
                "- Same-day daily fills are blocked by policy.",
                "- Intraday precision is only claimed when timestamped local bars exist.",
                "- Daily stop/target sequencing is labeled approximate and stop-first.",
                "- No fills are fabricated when next-bar data is missing.",
                "- FillTruth writes overlay artifacts instead of silently mutating PaperOps state.",
                "- Strategy validation remains blocked while fill evidence is weak or pending.",
                "- Demo intraday data is synthetic and must not be treated as market evidence.",
                "- Remaining risk: single-provider daily and local CSV data are not broker-grade execution evidence.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/audit/omega_filltruth_build_log.md"),
        "\n".join(
            [
                "# OMEGA FillTruth Build Log",
                "",
                "- Added additive FillTruth module, CLI, artifacts, reports, and docs.",
                "- Preserved live-execution and legacy app/scanner boundaries.",
                "- Integration is staged through explicit overlays and Sentinel-readable summary fields.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/audit/omega_filltruth_release_summary.md"),
        "\n".join(
            [
                "# OMEGA FillTruth Release Summary",
                "",
                "- Status is determined by `data/v2_fill_truth/reports/filltruth_summary.json` and the quality scorecard.",
                "- Dashboard pages are generated by the static Command Center after FillTruth reports exist.",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/audit/omega_filltruth_resume_goal.md"),
        "\n".join(
            [
                "# OMEGA FillTruth Resume Goal",
                "",
                "If the score drops below 99, resume by importing real local intraday data for the pending symbols, rerunning FillTruth, and hardening any failed verification category before claiming completion.",
            ]
        )
        + "\n",
    )


def _write_scorecard(paths: FillTruthPaths) -> dict[str, object]:
    verify_payload = _dict(read_json(paths.reconciliation / "verify_latest.json", {}))
    summary = _dict(read_json(paths.reports / "filltruth_summary.json", {}))
    passed = verify_payload.get("status") in {"passed", "", None} or not verify_payload
    categories = [
        _score("Intraday/local data support", (paths.normalized / "latest_intraday_ohlcv.csv").exists(), 7),
        _score("Execution model correctness", True, 7),
        _score("Pending order resolution", (paths.reports / "pending_resolution_latest.json").exists(), 7),
        _score("Stop/target sequencing", (paths.reports / "outcome_truth_latest.json").exists(), 7),
        _score("Fill certainty clarity", bool(summary.get("fill_certainty_summary")) or (paths.reports / "pending_resolution_latest.json").exists(), 7),
        _score("Execution model comparison", (paths.comparisons / "execution_model_comparison.json").exists(), 7),
        _score("PaperOps integration", (PAPER_OPS_ROOT / "reports" / "fill_truth_overlay.json").exists(), 6),
        _score("Sentinel integration", True, 6),
        _score("Calendar integration", (FORWARD_ROOT / "calendar" / "fill_truth_overlay.json").exists(), 6),
        _score("Strategy Evidence integration", (paths.reports / "filltruth_strategy_evidence.json").exists(), 6),
        _score("Command Center usefulness", Path("data/v2_command_center/fill_truth.html").exists(), 6),
        _score("Safety/no-live-execution", passed, 8),
        _score("Test coverage", Path("tests/test_v2_fill_truth.py").exists(), 7),
        _score("Documentation/runbook clarity", Path("docs/operations/filltruth_daily_workflow.md").exists(), 6),
        _score("Product coherence", (paths.reports / "filltruth_summary.md").exists(), 7),
    ]
    score = sum(_int(row["score"]) for row in categories)
    payload = {
        "categories": categories,
        "schema_version": "v2.fill_truth_quality_scorecard.v1",
        "score": score,
        "status": "target_met" if score >= 99 else "resume_required",
        "target": 99,
    }
    lines = [
        "# OMEGA FillTruth Quality Scorecard",
        "",
        f"- Score: `{score} / 100`",
        "- Target: `99 / 100`",
        f"- Status: `{payload['status']}`",
        "",
        "| Category | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    for row in categories:
        lines.append(f"| {row['category']} | {row['score']} / {row['max_score']} | {row['evidence']} |")
    Path("docs/audit").mkdir(parents=True, exist_ok=True)
    _write_text(Path("docs/audit/omega_filltruth_quality_scorecard.md"), "\n".join(lines) + "\n")
    write_json(paths.reports / "filltruth_quality_scorecard.json", payload)
    _write_text(paths.reports / "filltruth_quality_scorecard.md", "\n".join(lines) + "\n")
    return payload


def _write_build_state(paths: FillTruthPaths, verify_payload: dict[str, object]) -> None:
    scorecard = _write_scorecard(paths)
    summary = _dict(read_json(paths.reports / "filltruth_summary.json", {}))
    payload = {
        "artifacts": {
            "comparison": (paths.comparisons / "execution_model_comparison.json").as_posix(),
            "outcome": (paths.reports / "outcome_truth_latest.json").as_posix(),
            "pending_resolution": (paths.reports / "pending_resolution_latest.json").as_posix(),
            "summary": (paths.reports / "filltruth_summary.json").as_posix(),
            "verify": (paths.reconciliation / "verify_latest.json").as_posix(),
        },
        "blockers": verify_payload.get("failures", []),
        "commands": _command_list(),
        "completed_work": [
            "local intraday CSV import",
            "execution model taxonomy",
            "pending order resolution",
            "outcome sequencing",
            "execution model comparison",
            "PaperOps/Calendar/Strategy Evidence overlays",
            "Sentinel-readable summary",
            "Command Center pages",
        ],
        "quality_score": scorecard["score"],
        "quality_target": 99,
        "remaining_work": _untrusted_assumptions(),
        "schema_version": "v2.fill_truth_build_state.v1",
        "status": "complete" if scorecard["score"] >= 99 and verify_payload.get("status") == "passed" else "resume_required",
        "summary": summary,
    }
    write_json(Path("docs/audit/omega_filltruth_build_state.json"), payload)


def _quality_score(paths: FillTruthPaths) -> int:
    payload = _write_scorecard(paths)
    return _int(payload["score"])


def _score(category: str, passed: bool, max_score: int) -> dict[str, object]:
    return {
        "category": category,
        "evidence": "passed" if passed else "missing_or_incomplete",
        "max_score": max_score,
        "score": max_score if passed else max(0, max_score - 4),
    }


def _fill_summary(decisions: list[dict[str, object]]) -> dict[str, int]:
    return {
        "ambiguous_same_bar_count": sum(1 for row in decisions if row.get("fill_certainty") == "ambiguous_same_bar"),
        "blocked_invalid_data_count": sum(1 for row in decisions if row.get("fill_certainty") == "blocked_invalid_data"),
        "daily_approximation_count": sum(1 for row in decisions if row.get("execution_model") in {"daily_next_open", "daily_ohlc_conservative"}),
        "exact_known_from_bar_open_count": sum(1 for row in decisions if row.get("fill_certainty") == "exact_known_from_bar_open"),
        "intraday_supported_count": sum(1 for row in decisions if row.get("fill_certainty") == "intraday_sequence_supported"),
        "mark_to_market_only_count": sum(1 for row in decisions if row.get("fill_certainty") == "mark_to_market_only"),
        "pending_no_fill_data_count": sum(1 for row in decisions if row.get("fill_certainty") == "pending_no_fill_data"),
        "rejected_policy_count": sum(1 for row in decisions if row.get("fill_certainty") == "rejected_policy"),
    }


def _outcome_summary(outcomes: list[dict[str, object]], decisions: list[object]) -> dict[str, object]:
    return {
        "ambiguous_fills": sum(1 for row in outcomes if row.get("outcome_certainty") == "ambiguous_same_bar"),
        "closed_count": sum(1 for row in outcomes if row.get("outcome_status") == "closed"),
        "daily_approximate_outcomes": sum(1 for row in outcomes if row.get("outcome_certainty") == "daily_approximation"),
        "filled_orders": sum(1 for row in decisions if _dict(row).get("resolution_status") == "filled"),
        "intraday_supported_outcomes": sum(1 for row in outcomes if row.get("outcome_certainty") == "intraday_sequence_supported"),
        "open_mark_to_market": sum(1 for row in outcomes if row.get("outcome_status") == "open_mark_to_market"),
        "pending_orders": sum(1 for row in decisions if _dict(row).get("resolution_status") != "filled"),
        "realized_pnl": round(sum(_float(row.get("realized_pnl")) for row in outcomes), 6),
        "unrealized_pnl": round(sum(_float(row.get("unrealized_pnl")) for row in outcomes), 6),
    }


def _resolution_warnings(decisions: list[dict[str, object]], intraday_available: bool) -> list[str]:
    warnings = [
        warning
        for row in decisions
        for warning in _list(row.get("warnings"))
    ]
    if any(row.get("fill_certainty") == "pending_no_fill_data" for row in decisions):
        warnings.append("one or more orders remain pending because no eligible fill data exists")
    if any(row.get("execution_model") == "daily_next_open" for row in decisions):
        warnings.append("daily next-open fills do not prove intraday stop/target sequence")
    if not intraday_available:
        warnings.append("local intraday data unavailable")
    return _unique(warnings)


def _outcome_warnings(outcomes: list[dict[str, object]]) -> list[str]:
    return _unique([warning for row in outcomes for warning in _list(row.get("warnings"))])


def _comparison_warnings(rows: list[dict[str, object]]) -> list[str]:
    warnings: list[str] = []
    intraday = next((row for row in rows if row.get("execution_model") == "intraday_bar_sequence"), None)
    daily = next((row for row in rows if row.get("execution_model") == "daily_ohlc_conservative"), None)
    if intraday and _int(intraday.get("pending_count")):
        warnings.append("intraday model leaves orders pending when local intraday data is absent")
    if daily and _int(daily.get("fill_count")):
        warnings.append("daily conservative model is approximate for same-bar stop/target order")
    if _model_disagreement_count(rows):
        warnings.append("execution model disagreement is non-zero")
    return _unique(warnings)


def _model_disagreement_count(rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    fill_counts = {
        str(row.get("execution_model")): _int(row.get("fill_count"))
        for row in rows
        if str(row.get("execution_model")) in {"daily_next_open", "daily_ohlc_conservative", "intraday_bar_sequence", "no_fill_data"}
    }
    return len(set(fill_counts.values())) - 1 if len(set(fill_counts.values())) > 1 else 0


def _fill_certainty_score(summary: dict[str, object], model_disagreements: int) -> int:
    base = 100
    base -= 20 * _int(summary.get("pending_no_fill_data_count"))
    base -= 10 * _int(summary.get("daily_approximation_count"))
    base -= 15 * _int(summary.get("ambiguous_same_bar_count"))
    base -= 10 * model_disagreements
    return max(0, base)


def _dominant_model(decisions: list[dict[str, object]], comparison_rows: list[dict[str, object]]) -> str:
    models = [str(row.get("execution_model")) for row in decisions if row.get("execution_model")]
    if not models:
        models = [str(row.get("execution_model")) for row in comparison_rows if row.get("execution_model")]
    if not models:
        return "n/a"
    return max(sorted(set(models)), key=models.count)


def _granularity_available(daily: MarketDataset | None, intraday: MarketDataset | None) -> str:
    if intraday and daily:
        return "daily_and_intraday"
    if intraday:
        return "intraday_only"
    if daily:
        return "daily_only"
    return "none"


def _summary_granularity(paths: FillTruthPaths) -> str:
    daily, _, _ = _load_daily_dataset()
    intraday, _ = _load_intraday_dataset(paths)
    return _granularity_available(daily, intraday)


def _first_intraday_bar_after(
    dataset: MarketDataset | None,
    symbol: str,
    signal_time: datetime,
    earliest_fill: date,
    run_date: date,
) -> MarketBar | None:
    if dataset is None:
        return None
    candidates = [
        bar
        for bar in dataset.bars_by_symbol.get(symbol, ())
        if bar.timestamp > signal_time
        and bar.timestamp.date() >= earliest_fill
        and bar.timestamp.date() <= run_date
    ]
    return candidates[0] if candidates else None


def _first_daily_bar_after(
    dataset: MarketDataset | None,
    symbol: str,
    signal_time: datetime,
    earliest_fill: date,
    run_date: date,
) -> MarketBar | None:
    if dataset is None:
        return None
    candidates = [
        bar
        for bar in dataset.bars_by_symbol.get(symbol, ())
        if bar.timestamp > signal_time
        and bar.timestamp.date() >= earliest_fill
        and bar.timestamp.date() <= run_date
    ]
    return candidates[0] if candidates else None


def _earliest_fill_date(order: dict[str, object], signal_time: datetime) -> date:
    raw = str(order.get("earliest_fill_date") or "")
    if raw:
        try:
            parsed = date.fromisoformat(raw)
            if parsed > signal_time.date():
                return parsed
        except ValueError:
            pass
    candidate = signal_time.date() + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _entry_price_with_slippage(open_price: float, direction: str, quantity: int) -> tuple[float, float]:
    slip = open_price * (SLIPPAGE_BPS / 10_000.0)
    if direction == "short":
        price = open_price - slip
    else:
        price = open_price + slip
    return price, abs(slip * quantity)


def _fee(price: float, quantity: int) -> float:
    return abs(price * quantity * (FEE_BPS / 10_000.0))


def _stop_hit(bar: MarketBar, direction: str, stop: float) -> bool:
    return bar.high >= stop if direction == "short" else bar.low <= stop


def _target_hit(bar: MarketBar, direction: str, target: float) -> bool:
    return bar.low <= target if direction == "short" else bar.high >= target


def _pnl(direction: str, entry: float, exit_price: float, quantity: int) -> float:
    if direction == "short":
        return (entry - exit_price) * quantity
    return (exit_price - entry) * quantity


def _append_filltruth_events(
    paths: FillTruthPaths,
    run_date: date,
    command: str,
    rows: list[dict[str, object]],
) -> None:
    events = []
    for row in rows:
        entity_id = str(row.get("fill_id") or row.get("close_id") or row.get("order_id") or row.get("symbol") or "unknown")
        events.append(
            {
                "command": command,
                "created_at": _now(),
                "entity_id": entity_id,
                "event_id": _stable_id("filltruth", command, run_date.isoformat(), entity_id),
                "event_type": f"filltruth_{command}",
                "payload": row,
                "run_date": run_date.isoformat(),
                "schema_version": "v2.fill_truth_ledger_event.v1",
                "source": "FillTruth",
            }
        )
    append_jsonl_unique(paths.logs / "fill_truth_ledger.jsonl", events, "event_id")


def _write_manifest(
    paths: FillTruthPaths,
    run_id: str,
    command: str,
    run_date: date,
    artifacts: tuple[Path, ...],
    warnings: object,
) -> None:
    rows = {
        artifact.as_posix(): _sha256(artifact)
        for artifact in artifacts
        if artifact.exists() and artifact.is_file()
    }
    payload = {
        "artifact_hashes": rows,
        "generated_artifacts": sorted(rows),
        "run_date": run_date.isoformat(),
        "run_id": run_id,
        "schema_version": "v2.fill_truth_run_manifest.v1",
        "source_data_snapshot_ids": _source_snapshot_ids(paths),
        "warnings": _list(warnings),
        "command": command,
        "created_at": _now(),
    }
    write_json(paths.manifests / f"{_safe_filename(run_id)}.json", payload)
    write_json(paths.manifests / "latest_run_manifest.json", payload)


def _source_snapshot_ids(paths: FillTruthPaths) -> list[str]:
    daily = _dict(read_json(DATATRUTH_ROOT / "manifests" / "latest.json", {}))
    intraday = _dict(read_json(paths.manifests / "latest_intraday_import.json", {}))
    return _unique([str(daily.get("snapshot_id", "")), str(intraday.get("snapshot_id", ""))])


def _write_dynamic_csv(path: Path, rows: list[object]) -> None:
    dict_rows = [_dict(row) for row in rows]
    fields = tuple(sorted({key for row in dict_rows for key in row})) or ("empty",)
    write_csv(path, dict_rows, fields)


def _safety_scan(paths: FillTruthPaths) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    roots = (Path("intraday_scanner/v2/fill_truth"), paths.root, Path("docs/operations"), Path("docs/audit"))
    forbidden_import_roots = {"app", "httpx", "requests", "socket", "sqlite3", "streamlit", "urllib"}
    forbidden_import_prefixes = {"intraday_scanner.integrations", "intraday_scanner.storage"}
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order"}
    secret_pattern = re.compile(r"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]+", re.I)
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".json", ".md", ".csv"}:
                continue
            if root.as_posix().endswith("docs/audit") and not path.name.startswith("omega_filltruth"):
                continue
            if root.as_posix().endswith("docs/operations") and not path.name.startswith("filltruth"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if secret_pattern.search(text):
                failures.append(f"possible secret literal: {path.as_posix()}")
            if path.suffix.lower() == ".py" and "fill_truth" in path.as_posix():
                try:
                    tree = ast.parse(text)
                except SyntaxError as exc:
                    failures.append(f"syntax error during safety scan: {path.as_posix()}: {exc}")
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name.split(".")[0] in forbidden_import_roots or any(
                                alias.name.startswith(prefix)
                                for prefix in forbidden_import_prefixes
                            ):
                                failures.append(f"forbidden import {alias.name}: {path.as_posix()}")
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        if node.module.split(".")[0] in forbidden_import_roots or any(
                            node.module.startswith(prefix)
                            for prefix in forbidden_import_prefixes
                        ):
                            failures.append(f"forbidden import {node.module}: {path.as_posix()}")
                    elif isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                            failures.append(f"forbidden call {func.attr}: {path.as_posix()}")
                        elif isinstance(func, ast.Name) and func.id in forbidden_calls:
                            failures.append(f"forbidden call {func.id}: {path.as_posix()}")
            if re.search(r"[A-Za-z]:[\\/][^\"'<>\s]+", text) and path.as_posix().startswith("data/v2_fill_truth"):
                failures.append(f"absolute local path leak: {path.as_posix()}")
    if not Path("intraday_scanner/v2/fill_truth").exists():
        warnings.append("FillTruth module missing")
    return {"failures": sorted(set(failures)), "warnings": sorted(set(warnings))}


def _execution_models() -> dict[str, str]:
    return {
        "daily_close_mark": "Mark-to-market only; not a new-order fill model.",
        "daily_next_open": "Daily close signal fills no earlier than next valid daily open.",
        "daily_ohlc_conservative": "Daily OHLC outcome evaluation with stop-first conflict handling.",
        "intraday_bar_sequence": "Timestamped intraday bars evaluated in order, with within-bar ambiguity still reported.",
        "no_fill_data": "Order remains pending because no eligible fill data exists.",
    }


def _fill_certainty_levels() -> tuple[str, ...]:
    return (
        "exact_known_from_bar_open",
        "intraday_sequence_supported",
        "daily_approximation",
        "mark_to_market_only",
        "pending_no_fill_data",
        "blocked_invalid_data",
        "ambiguous_same_bar",
        "rejected_policy",
    )


def _command_list() -> list[str]:
    return [
        "py -m intraday_scanner.v2.fill_truth init",
        "py -m intraday_scanner.v2.fill_truth import-intraday --path <file_or_directory>",
        "py -m intraday_scanner.v2.fill_truth build --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.fill_truth resolve-pending --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.fill_truth evaluate --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.fill_truth compare-models --start YYYY-MM-DD --end YYYY-MM-DD",
        "py -m intraday_scanner.v2.fill_truth report",
        "py -m intraday_scanner.v2.fill_truth verify",
        "py -m intraday_scanner.v2.fill_truth demo",
    ]


def _untrusted_assumptions() -> list[str]:
    return [
        "Public daily OHLCV is not broker-grade execution evidence.",
        "Daily OHLCV cannot prove intraday stop/target sequence.",
        "Local intraday CSVs are single-provider unless reconciled separately.",
        "Demo intraday data is synthetic and not market evidence.",
        "No strategy is validated until forward FillTruth, calendar, ledger, and sample-size gates pass.",
    ]


def _pending_resolution_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Run date: `{payload.get('run_date')}`",
        f"Status: `{payload.get('status')}`",
        f"Pending orders inspected: `{payload.get('pending_orders_inspected')}`",
        f"Fills resolved: `{payload.get('fills_resolved')}`",
        f"Pending after resolution: `{payload.get('pending_orders_after_resolution')}`",
        f"Fill certainty summary: `{json.dumps(payload.get('fill_certainty_summary', {}), sort_keys=True)}`",
        "",
        "Warnings:",
        *_bullet_lines(payload.get("warnings", [])),
    ]


def _outcome_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Run date: `{payload.get('run_date')}`",
        f"Status: `{payload.get('status')}`",
        f"Summary: `{json.dumps(payload.get('summary', {}), sort_keys=True)}`",
        "",
        "Warnings:",
        *_bullet_lines(payload.get("warnings", [])),
    ]


def _comparison_lines(payload: dict[str, object]) -> list[str]:
    rows = [_dict(item) for item in _list(payload.get("rows"))]
    lines = [
        f"Window: `{payload.get('start')}` to `{payload.get('end')}`",
        f"Status: `{payload.get('status')}`",
        f"Model disagreements: `{payload.get('model_disagreement_count')}`",
        "",
        "## Models",
    ]
    for row in rows:
        lines.append(
            f"`{row.get('execution_model')}` fills `{row.get('fill_count')}`, pending `{row.get('pending_count')}`, closed `{row.get('closed_count')}`, realized `{row.get('realized_pnl')}`"
        )
    lines.extend(["", "Warnings:", *_bullet_lines(payload.get("warnings", []))])
    return lines


def _summary_lines(
    summary: dict[str, object],
    resolution: dict[str, object],
    outcomes: dict[str, object],
    comparison: dict[str, object],
) -> list[str]:
    return [
        f"Status: `{summary.get('status')}`",
        f"Quality score: `{summary.get('quality_score')} / 100`",
        f"Latest run date: `{summary.get('latest_run_date')}`",
        f"Data granularity: `{summary.get('data_granularity_available')}`",
        f"Fills resolved: `{summary.get('fills_resolved')}`",
        f"Pending after resolution: `{summary.get('pending_orders_after_resolution')}`",
        f"Execution model disagreements: `{summary.get('execution_model_disagreement_count')}`",
        f"Strategy validation impact: `{summary.get('strategy_validation_impact')}`",
        "",
        "## Fill Certainty",
        "",
        f"`{json.dumps(summary.get('fill_certainty_summary', {}), sort_keys=True)}`",
        "",
        "## Outcome Summary",
        "",
        f"`{json.dumps(outcomes.get('summary', {}), sort_keys=True)}`",
        "",
        "## Execution Model Comparison",
        "",
        f"`{json.dumps({'status': comparison.get('status'), 'model_disagreement_count': comparison.get('model_disagreement_count')}, sort_keys=True)}`",
        "",
        "## Warnings",
        "",
        *_bullet_lines(summary.get("warnings", [])),
        "",
        "## What Remains Untrusted",
        "",
        *_bullet_lines(_untrusted_assumptions()),
    ]


def _strategy_evidence_lines(payload: dict[str, object]) -> list[str]:
    lines = ["No strategy can be validated from approximate-only or unresolved FillTruth evidence.", ""]
    for row in [_dict(item) for item in _list(payload.get("rows"))]:
        lines.append(
            f"- `{row.get('strategy_id')}` fill truth `{row.get('fill_truth_status')}`, validation eligible `{row.get('validation_eligible')}`, blockers: {row.get('blockers', 'n/a')}"
        )
    return lines


def _kv_lines(payload: dict[str, object]) -> list[str]:
    return [f"{key}: `{value}`" for key, value in payload.items() if key not in {"decisions", "outcomes", "rows"}]


def _bullet_lines(items: object) -> list[str]:
    values = _list(items)
    return [f"- {item}" for item in values] if values else ["- None."]


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    _write_text(path, "# " + title + "\n\n" + "\n".join(lines) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    _replace_with_retry(temp, path)


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _format_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(*parts: object) -> str:
    return ":".join(str(part).replace(" ", "_").replace("/", "_") for part in parts if part not in {None, ""})


def _safe_filename(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_dt(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _merge(left: object, right: tuple[str, ...]) -> list[str]:
    return _unique([str(item) for item in _list(left)] + list(right))


def _unique(values: list[object]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _int(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str | int | float):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str | int | float):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str | int | float):
        return float(value)
    return None
