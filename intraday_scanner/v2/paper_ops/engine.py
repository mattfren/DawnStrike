"""Scheduler-ready PaperOps v1 file-backed engine."""

# mypy: ignore-errors

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from intraday_scanner.v2.backtest import BacktestEngine, BacktestResult, BacktestSettings
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.data.synthetic import build_synthetic_ohlcv_dataset
from intraday_scanner.v2.data_truth import build_data_truth_snapshot
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.paper_ops.models import (
    PaperAccountState,
    PaperClose,
    PaperCloseReason,
    PaperFill,
    PaperJobPhase,
    PaperLedgerEvent,
    PaperOpsConfig,
    PaperOpsManifest,
    PaperOpsReconciliationReport,
    PaperOrder,
    PaperOrderStatus,
    PaperPick,
    PaperPickDecision,
    PaperPosition,
    PaperPositionStatus,
    PaperRun,
    PaperRunMode,
    PaperStrategyConfig,
    StrategyCalendarRow,
    StrategyPaperAccount,
    stable_id,
)
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    read_json,
    read_jsonl,
    upsert_rows,
    write_csv,
    write_json,
)
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.scanner import ScanOutput, run_latest_scan
from intraday_scanner.v2.strategies import Direction, StrategySpec, build_strategy_catalog

PAPER_TIMEOUT_DAYS = 10
_DATASET_CACHE: dict[
    tuple[str, str, bool],
    tuple[MarketDataset, DataTruthManifest, tuple[str, ...]],
] = {}


@dataclass(frozen=True)
class PaperOpsPaths:
    root: Path
    ledger: Path
    state: Path
    calendar: Path
    reports: Path
    manifests: Path
    logs: Path
    exports: Path
    reconciliation: Path

    @classmethod
    def create(cls, root: Path) -> PaperOpsPaths:
        paths = cls(
            root=root,
            ledger=root / "ledger",
            state=root / "state",
            calendar=root / "calendar",
            reports=root / "reports",
            manifests=root / "manifests",
            logs=root / "logs",
            exports=root / "exports",
            reconciliation=root / "reconciliation",
        )
        for path in (
            paths.root,
            paths.ledger,
            paths.state,
            paths.calendar,
            paths.reports,
            paths.manifests,
            paths.logs,
            paths.exports,
            paths.reconciliation,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (paths.reports / "daily").mkdir(parents=True, exist_ok=True)
        return paths


def init(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    config = PaperOpsConfig()
    write_json(paths.state / "paper_ops_config.json", config.to_dict())
    registry = [config_row.to_dict() for config_row in _strategy_configs(config)]
    write_json(paths.state / "strategy_registry.json", registry)
    if not (paths.state / "pending_orders.json").exists():
        write_json(paths.state / "pending_orders.json", [])
    if not (paths.state / "open_positions.json").exists():
        write_json(paths.state / "open_positions.json", [])
    if not (paths.state / "paper_accounts.json").exists():
        write_json(paths.state / "paper_accounts.json", _fresh_account_payload(paths, config))
    _repair_legacy_default_state(paths, config)
    return {"status": "initialized", "output_root": output_root.as_posix()}


def preflight(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    init(output_root=output_root)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    payload = {
        "data_snapshot_id": manifest.snapshot_id,
        "latest_completed_date": manifest.accepted_end,
        "mode": mode.value,
        "run_date": run_date.isoformat(),
        "run_id": run.run_id,
        "status": "passed_with_warnings" if warnings else "passed",
        "symbols": list(dataset.symbols),
        "warnings": list(warnings),
    }
    paths = PaperOpsPaths.create(output_root)
    write_json(paths.exports / f"preflight_{mode.value}_{run_date.isoformat()}.json", payload)
    _write_manifest(
        paths, run, (paths.exports / f"preflight_{mode.value}_{run_date}.json",), warnings
    )
    return payload


def scan(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    config = _config(paths)
    strategies = tuple(
        strategy for strategy in build_strategy_catalog() if _eligible(strategy, config)
    )
    results = _backtest_results(dataset, strategies)
    scan_output = run_latest_scan(
        dataset,
        strategies,
        results,
        risk_settings=RiskSettings(account_equity=config.starting_equity),
        data_snapshot_id=manifest.snapshot_id,
        run_manifest_id=run.run_id,
    )
    picks = _picks_from_scan(scan_output, strategies, run, config, warnings)
    picks_path = paths.exports / f"picks_{mode.value}_{run_date.isoformat()}.json"
    write_json(picks_path, [pick.to_dict() for pick in picks])
    _append_events(
        paths,
        [
            _event(
                run,
                PaperJobPhase.SCAN,
                pick.strategy_id,
                pick.symbol,
                "paper_pick_decision",
                pick.pick_id,
                pick.to_dict(),
            )
            for pick in picks
        ],
    )
    accepted = [pick for pick in picks if pick.decision is PaperPickDecision.ACCEPTED]
    _write_daily_report(
        paths,
        run,
        manifest,
        {"picks": len(picks), "accepted_picks": len(accepted), "phase": "scan"},
        warnings,
    )
    return {
        "accepted_picks": len(accepted),
        "data_snapshot_id": manifest.snapshot_id,
        "picks": len(picks),
        "run_id": run.run_id,
        "warnings": list(warnings),
    }


def enter(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    config = _config(paths)
    _repair_legacy_default_state(paths, config)
    picks = _load_picks(paths, mode, run_date)
    if not picks:
        scan(run_date=run_date, mode=mode, output_root=output_root, allow_fetch=allow_fetch)
        picks = _load_picks(paths, mode, run_date)
    pending_path = _pending_orders_path(paths, mode)
    pending = _repair_pending_order_rows(_state_rows(pending_path, mode), dataset)
    existing_ids = {str(row.get("order_id")) for row in pending}
    orders = [
        order
        for pick in picks
        if pick.decision is PaperPickDecision.ACCEPTED
        for order in [_order_from_pick(pick, run, config, dataset)]
        if order.order_id not in existing_ids
    ]
    pending.extend(order.to_dict() for order in orders)
    write_json(pending_path, pending)
    _append_events(
        paths,
        [
            _event(
                run,
                PaperJobPhase.ENTER,
                order.strategy_id,
                order.symbol,
                "paper_order_created",
                order.order_id,
                order.to_dict(),
            )
            for order in orders
        ],
    )
    _write_daily_report(
        paths,
        run,
        manifest,
        {"orders_created": len(orders), "pending_orders": len(pending), "phase": "enter"},
        warnings,
    )
    return {"orders_created": len(orders), "pending_orders": len(pending), "run_id": run.run_id}


def check(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    config = _config(paths)
    _repair_legacy_default_state(paths, config)
    pending_path = _pending_orders_path(paths, mode)
    positions_path = _open_positions_path(paths, mode)
    pending_rows = _repair_pending_order_rows(_state_rows(pending_path, mode), dataset)
    position_rows = _state_rows(positions_path, mode)
    accounts = _accounts(paths, mode)
    filled_orders: set[str] = set()
    new_positions: list[PaperPosition] = []
    fills: list[PaperFill] = []

    for order in [_order_from_row(row) for row in pending_rows]:
        fill_bar = _next_bar_after(dataset, order.symbol, order.signal_time, run_date)
        if fill_bar is None:
            _append_events(
                paths,
                [
                    _event(
                        run,
                        PaperJobPhase.CHECK,
                        order.strategy_id,
                        order.symbol,
                        "paper_order_pending_no_fill_data",
                        f"{order.order_id}:pending_check:{run_date}",
                        order.to_dict(),
                    )
                ],
            )
            continue
        fill = _fill_order(order, fill_bar, run, config)
        position = _position_from_fill(order, fill)
        fills.append(fill)
        new_positions.append(position)
        filled_orders.add(order.order_id)
    remaining_pending = [
        row for row in pending_rows if str(row.get("order_id")) not in filled_orders
    ]
    write_json(pending_path, remaining_pending)

    closes: list[PaperClose] = []
    checked_no_action: list[PaperPosition] = []
    updated_positions: list[dict[str, object]] = []
    for position in [
        _position_from_row(row) for row in position_rows + [p.to_dict() for p in new_positions]
    ]:
        bar = _latest_bar_on_or_before(dataset, position.symbol, run_date)
        if bar is None:
            updated_positions.append(position.to_dict())
            continue
        checked, close_record = _check_position(position, bar, run, config)
        if close_record is not None:
            closes.append(close_record)
            accounts = _apply_close(accounts, close_record)
        else:
            updated_positions.append(checked.to_dict())
            checked_no_action.append(checked)
            accounts = _apply_mark(accounts, checked)

    accounts = _recalculate_unrealized_accounts(accounts, updated_positions)
    write_json(positions_path, updated_positions)
    _write_accounts(paths, mode, accounts)
    _append_events(
        paths,
        [
            _event(
                run,
                PaperJobPhase.CHECK,
                fill.strategy_id,
                fill.symbol,
                "paper_fill",
                fill.fill_id,
                fill.to_dict(),
            )
            for fill in fills
        ]
        + [
            _event(
                run,
                PaperJobPhase.CHECK,
                position.strategy_id,
                position.symbol,
                "paper_position_opened",
                position.position_id,
                position.to_dict(),
            )
            for position in new_positions
        ]
        + [
            _event(
                run,
                PaperJobPhase.CHECK,
                close_record.strategy_id,
                close_record.symbol,
                "paper_position_closed",
                close_record.close_id,
                close_record.to_dict(),
            )
            for close_record in closes
        ],
    )
    _append_events(
        paths,
        [
            _event(
                run,
                PaperJobPhase.CHECK,
                position.strategy_id,
                position.symbol,
                "paper_position_checked_no_action",
                f"{position.position_id}:checked:{run_date}",
                position.to_dict(),
            )
            for position in checked_no_action
        ],
    )
    _write_calendar_for_date(paths, run, manifest, warnings)
    _write_daily_report(
        paths,
        run,
        manifest,
        {
            "fills": len(fills),
            "closes": len(closes),
            "open_positions": len(updated_positions),
            "phase": "check",
        },
        warnings,
    )
    return {
        "closes": len(closes),
        "fills": len(fills),
        "open_positions": len(updated_positions),
        "run_id": run.run_id,
    }


def close(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    config = _config(paths)
    _repair_legacy_default_state(paths, config)
    if mode is PaperRunMode.FORWARD:
        extra_warning = (
            "daily forward mode does not claim intraday EOD-flat closeout; carry "
            "positions are marked to latest completed close"
        )
    else:
        extra_warning = "daily replay/demo closeout is labeled research-only"
    warnings = tuple(dict.fromkeys(list(warnings) + [extra_warning]))
    positions_path = _open_positions_path(paths, mode)
    position_rows = _state_rows(positions_path, mode)
    accounts = _accounts(paths, mode)
    updated_positions: list[dict[str, object]] = []
    closes: list[PaperClose] = []
    marks: list[PaperPosition] = []
    for position in [_position_from_row(row) for row in position_rows]:
        bar = _latest_bar_on_or_before(dataset, position.symbol, run_date)
        if bar is None:
            updated_positions.append(position.to_dict())
            continue
        checked, close_record = _check_position(position, bar, run, config)
        if close_record is not None:
            closes.append(close_record)
            accounts = _apply_close(accounts, close_record)
        else:
            updated_positions.append(checked.to_dict())
            marks.append(checked)
            accounts = _apply_mark(accounts, checked)
    accounts = _recalculate_unrealized_accounts(accounts, updated_positions)
    write_json(positions_path, updated_positions)
    _write_accounts(paths, mode, accounts)
    _append_events(
        paths,
        [
            _event(
                run,
                PaperJobPhase.CLOSE,
                close_record.strategy_id,
                close_record.symbol,
                "paper_position_closed",
                close_record.close_id,
                close_record.to_dict(),
            )
            for close_record in closes
        ]
        + [
            _event(
                run,
                PaperJobPhase.CLOSE,
                position.strategy_id,
                position.symbol,
                "paper_position_marked_to_market",
                f"{position.position_id}:mark:{run_date}",
                position.to_dict(),
            )
            for position in marks
        ],
    )
    _write_calendar_for_date(paths, run, manifest, warnings)
    _write_daily_report(
        paths,
        run,
        manifest,
        {"closes": len(closes), "marked_positions": len(marks), "phase": "close"},
        warnings,
    )
    return {
        "closes": len(closes),
        "marked_positions": len(marks),
        "run_id": run.run_id,
        "warnings": list(warnings),
    }


def run_day(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    preflight(run_date=run_date, mode=mode, output_root=output_root, allow_fetch=allow_fetch)
    scan_result = scan(
        run_date=run_date,
        mode=mode,
        output_root=output_root,
        allow_fetch=allow_fetch,
    )
    enter_result = enter(
        run_date=run_date,
        mode=mode,
        output_root=output_root,
        allow_fetch=allow_fetch,
    )
    check_result = check(
        run_date=run_date,
        mode=mode,
        output_root=output_root,
        allow_fetch=allow_fetch,
    )
    close_result = close(
        run_date=run_date,
        mode=mode,
        output_root=output_root,
        allow_fetch=allow_fetch,
    )
    calendar_result = calendar(output_root=output_root)
    reconciliation = reconcile(output_root=output_root)
    report_result = report(output_root=output_root)
    return {
        "calendar": calendar_result,
        "check": check_result,
        "close": close_result,
        "enter": enter_result,
        "reconcile": reconciliation,
        "report": report_result,
        "run_id": scan_result["run_id"],
        "scan": scan_result,
    }


def replay(
    *,
    start: date,
    end: date,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    config = _config(paths)
    _reset_mode_generated_state(paths, PaperRunMode.REPLAY, config)
    current = start
    days = 0
    while current <= end:
        run_day(
            run_date=current,
            mode=PaperRunMode.REPLAY,
            output_root=output_root,
            allow_fetch=allow_fetch,
        )
        current += timedelta(days=1)
        days += 1
    return {"mode": PaperRunMode.REPLAY.value, "days": days}


def calendar(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    rows = _read_calendar_rows(paths)
    _write_calendar_matrix(paths, rows)
    _write_monthly_returns(paths, rows)
    _write_equity_and_drawdown(paths, rows)
    _write_calendar_summary(paths, rows)
    return {"calendar_rows": len(rows)}


def reconcile(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    event_ids = [str(event.get("event_id")) for event in events]
    duplicates = sorted({event_id for event_id in event_ids if event_ids.count(event_id) > 1})
    order_ids = {
        str(event.get("payload", {}).get("order_id"))
        for event in events
        if event.get("event_type") == "paper_order_created"
        and isinstance(event.get("payload"), dict)
    }
    fill_orphans = tuple(
        str(event.get("payload", {}).get("fill_id"))
        for event in events
        if event.get("event_type") == "paper_fill"
        and isinstance(event.get("payload"), dict)
        and str(event.get("payload", {}).get("order_id")) not in order_ids
    )
    position_ids = {
        str(event.get("payload", {}).get("position_id"))
        for event in events
        if event.get("event_type") == "paper_position_opened"
        and isinstance(event.get("payload"), dict)
    }
    close_orphans = tuple(
        str(event.get("payload", {}).get("close_id"))
        for event in events
        if event.get("event_type") == "paper_position_closed"
        and isinstance(event.get("payload"), dict)
        and str(event.get("payload", {}).get("position_id")) not in position_ids
    )
    status = "passed" if not duplicates and not fill_orphans and not close_orphans else "failed"
    report_payload = PaperOpsReconciliationReport(
        report_id="paper_ops_reconciliation_latest",
        run_id="latest",
        status=status,
        duplicate_event_ids=tuple(duplicates),
        orphan_fills=fill_orphans,
        orphan_closes=close_orphans,
        calendar_mismatches=(),
        warnings=(),
    )
    write_json(paths.reconciliation / "reconciliation_latest.json", report_payload.to_dict())
    _write_reconciliation_markdown(paths, report_payload)
    return report_payload.to_dict()


def report(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    rows = _read_calendar_rows(paths)
    lines = [
        "# PaperOps v1 Summary",
        "",
        f"- Calendar rows: `{len(rows)}`",
        f"- Forward days tracked: `{len({r['date'] for r in rows if r['mode'] == 'forward'})}`",
        f"- Replay days tracked: `{len({r['date'] for r in rows if r['mode'] == 'replay'})}`",
        "",
        "## Strategy Status",
        "",
    ]
    for strategy_id in sorted({str(row["strategy_id"]) for row in rows}):
        strategy_rows = [row for row in rows if row["strategy_id"] == strategy_id]
        last = strategy_rows[-1]
        lines.append(
            f"- `{strategy_id}` cumulative return `{last['cumulative_return_pct']}`; "
            f"drawdown `{last['drawdown_pct']}`."
        )
    lines.extend(
        [
            "",
            "## Data Limitations",
            "",
            "- Forward mode uses validated DataTruth snapshots only.",
            "- Single-provider public data remains unreconciled until a second comparable "
            "source exists.",
            "- Daily-data forward mode carries positions; it does not claim intraday "
            "EOD-flat precision.",
        ]
    )
    path = paths.reports / "paper_ops_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"summary": path.as_posix()}


def demo(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    init(output_root=output_root)
    dataset = build_synthetic_ohlcv_dataset(end_date=date(2026, 6, 29), trading_days=130)
    manifest = DataTruthManifest(
        snapshot_id="paper_ops_demo_synthetic_snapshot",
        created_at=datetime(2026, 6, 29, tzinfo=timezone.utc).isoformat(),
        provider_id="synthetic",
        provider_name="PaperOps deterministic demo fixture",
        symbols=dataset.symbols,
        timeframe="1d",
        requested_start="2026-01-01",
        requested_end="2026-06-29",
        accepted_start="2026-01-01",
        accepted_end="2026-06-29",
        bar_count=dataset.total_bars,
        accepted_bar_count=dataset.total_bars,
        rejected_bar_count=0,
        skipped_incomplete_bars=0,
        validation_status="demo_only",
        warnings=("synthetic demo data; not market evidence",),
        raw_artifact_hashes={},
        normalized_artifact_hash="demo",
        source_url_or_reference=("generated:paper_ops_demo",),
    )
    run = _paper_run(
        run_date=date(2026, 6, 29), mode=PaperRunMode.DEMO, data_snapshot_id=manifest.snapshot_id
    )
    paths = PaperOpsPaths.create(output_root)
    write_json(paths.exports / "demo_dataset_marker.json", {"snapshot_id": manifest.snapshot_id})
    _write_calendar_for_date(paths, run, manifest, manifest.warnings)
    calendar(output_root=output_root)
    reconcile(output_root=output_root)
    report(output_root=output_root)
    return {"mode": "demo", "run_id": run.run_id, "snapshot_id": manifest.snapshot_id}


def _load_dataset_for_mode(
    *,
    run_date: date,
    mode: PaperRunMode,
    allow_fetch: bool = True,
) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
    if mode is PaperRunMode.DEMO:
        dataset = build_synthetic_ohlcv_dataset(end_date=run_date, trading_days=130)
        manifest = DataTruthManifest(
            snapshot_id="paper_ops_demo_synthetic_snapshot",
            created_at=datetime.now(timezone.utc).isoformat(),
            provider_id="synthetic",
            provider_name="PaperOps deterministic demo fixture",
            symbols=dataset.symbols,
            timeframe="1d",
            requested_start="n/a",
            requested_end=run_date.isoformat(),
            accepted_start="n/a",
            accepted_end=run_date.isoformat(),
            bar_count=dataset.total_bars,
            accepted_bar_count=dataset.total_bars,
            rejected_bar_count=0,
            skipped_incomplete_bars=0,
            validation_status="demo_only",
            warnings=("synthetic demo data; not market evidence",),
            raw_artifact_hashes={},
            normalized_artifact_hash="demo",
            source_url_or_reference=("generated:paper_ops_demo",),
        )
        return dataset, manifest, manifest.warnings
    cache_key = (mode.value, run_date.isoformat(), allow_fetch)
    cached = _DATASET_CACHE.get(cache_key)
    if cached is not None:
        return cached
    snapshot_as_of = run_date + timedelta(days=1) if mode is PaperRunMode.REPLAY else run_date
    result = build_data_truth_snapshot(as_of_date=snapshot_as_of, allow_fetch=allow_fetch)
    if mode is PaperRunMode.FORWARD and (
        result.dataset.source_kind == "synthetic" or result.manifest.provider_id == "synthetic"
    ):
        raise ValueError("forward PaperOps rejects synthetic data snapshots")
    if mode is PaperRunMode.FORWARD and result.reconciliation.status in {
        "mismatch",
        "provider_disagreement",
        "insufficient_overlap",
        "provider_error",
    }:
        raise ValueError(
            f"forward PaperOps blocks DataTruth status {result.reconciliation.status}"
        )
    warnings = tuple(result.manifest.warnings) + tuple(result.reconciliation.warnings)
    loaded = (result.dataset, result.manifest, tuple(dict.fromkeys(warnings)))
    _DATASET_CACHE[cache_key] = loaded
    return loaded


def _paper_run(*, run_date: date, mode: PaperRunMode, data_snapshot_id: str) -> PaperRun:
    return PaperRun(
        run_id=stable_id("paper_ops", mode.value, run_date.isoformat(), data_snapshot_id),
        mode=mode,
        run_date=run_date.isoformat(),
        data_snapshot_id=data_snapshot_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _strategy_configs(config: PaperOpsConfig) -> tuple[PaperStrategyConfig, ...]:
    configs: list[PaperStrategyConfig] = []
    for strategy in build_strategy_catalog():
        if strategy.status in {"baseline", "benchmark"}:
            continue
        allow_entries = strategy.status in {"candidate", "validated"} or (
            strategy.status == "experimental" and config.allow_experimental
        )
        configs.append(
            PaperStrategyConfig(
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                strategy_status=strategy.status,
                paper_status="eligible" if allow_entries else "blocked",
                allow_entries=allow_entries,
                risk_per_trade_pct=config.risk_per_trade_pct,
                max_concurrent_positions=config.max_concurrent_positions,
            )
        )
    return tuple(configs)


def _eligible(strategy: StrategySpec, config: PaperOpsConfig) -> bool:
    if strategy.status in {"quarantined", "rejected", "parked", "baseline", "benchmark"}:
        return False
    if strategy.status == "experimental":
        return config.allow_experimental
    return True


def _backtest_results(
    dataset: MarketDataset,
    strategies: tuple[StrategySpec, ...],
) -> dict[str, BacktestResult]:
    settings = BacktestSettings(
        initial_capital=100_000.0,
        fee_bps=1.0,
        slippage_bps=5.0,
        risk=RiskSettings(account_equity=100_000.0),
    )
    engine = BacktestEngine(settings=settings)
    return {strategy.strategy_id: engine.run(strategy, dataset) for strategy in strategies}


def _picks_from_scan(
    scan_output: ScanOutput,
    strategies: tuple[StrategySpec, ...],
    run: PaperRun,
    config: PaperOpsConfig,
    inherited_warnings: tuple[str, ...],
) -> tuple[PaperPick, ...]:
    status_by_id = {strategy.strategy_id: strategy.status for strategy in strategies}
    picks: list[PaperPick] = []
    for card in scan_output.cards:
        entry = _entry_from_card(card.direction, card.stop, card.risk_per_share)
        reason = "accepted"
        decision = PaperPickDecision.ACCEPTED
        warnings = list(inherited_warnings) + list(card.warnings)
        if card.stop is None or card.risk_per_share is None or entry is None:
            decision = PaperPickDecision.REJECTED
            reason = "missing_or_invalid_entry_stop"
        elif card.target is None:
            decision = PaperPickDecision.REJECTED
            reason = "missing_target"
        elif card.reward_risk is not None and card.reward_risk < config.min_reward_risk:
            decision = PaperPickDecision.REJECTED
            reason = "reward_risk_below_threshold"
        pick_id = stable_id(
            run.mode.value,
            run.run_date,
            card.strategy_id,
            card.strategy_version,
            card.symbol,
            card.timestamp.isoformat(),
            card.direction,
        )
        picks.append(
            PaperPick(
                pick_id=pick_id,
                run_id=run.run_id,
                mode=run.mode,
                trade_date=run.run_date,
                strategy_id=card.strategy_id,
                strategy_version=card.strategy_version,
                strategy_status=status_by_id.get(card.strategy_id, "unknown"),
                symbol=card.symbol,
                signal_time=card.timestamp.isoformat(),
                direction=card.direction,
                setup_score=card.setup_score,
                entry_reference=entry or 0.0,
                stop=card.stop,
                target=card.target,
                risk_per_unit=card.risk_per_share,
                reward_per_unit=card.reward,
                reward_risk=card.reward_risk,
                decision=decision,
                reason=reason,
                evidence=card.evidence,
                warnings=tuple(dict.fromkeys(warnings)),
            )
        )
    return tuple(picks)


def _entry_from_card(direction: str, stop: float | None, risk: float | None) -> float | None:
    if stop is None or risk is None:
        return None
    if direction == Direction.LONG:
        return stop + risk
    return stop - risk


def _order_from_pick(
    pick: PaperPick,
    run: PaperRun,
    config: PaperOpsConfig,
    dataset: MarketDataset | None = None,
) -> PaperOrder:
    assert pick.stop is not None
    assert pick.risk_per_unit is not None
    quantity = max(
        int((config.starting_equity * config.risk_per_trade_pct) / pick.risk_per_unit), 0
    )
    earliest_fill = _next_valid_fill_date(
        symbol=pick.symbol,
        signal_time=pick.signal_time,
        dataset=dataset,
    ).isoformat()
    return PaperOrder(
        order_id=stable_id("order", pick.pick_id),
        pick_id=pick.pick_id,
        run_id=run.run_id,
        mode=run.mode,
        trade_date=run.run_date,
        strategy_id=pick.strategy_id,
        strategy_version=pick.strategy_version,
        symbol=pick.symbol,
        direction=pick.direction,
        order_status=PaperOrderStatus.PENDING,
        expected_fill_rule="daily signal fills no earlier than next valid bar open",
        signal_time=pick.signal_time,
        earliest_fill_date=earliest_fill,
        entry=pick.entry_reference,
        stop=pick.stop,
        target=pick.target,
        risk_per_unit=pick.risk_per_unit,
        reward_per_unit=pick.reward_per_unit,
        reward_risk=pick.reward_risk,
        risk_budget=config.starting_equity * config.risk_per_trade_pct,
        quantity=quantity,
        notional_exposure=quantity * pick.entry_reference,
        max_loss_estimate=quantity * pick.risk_per_unit,
        strategy_equity_basis=config.starting_equity,
        warnings=pick.warnings,
    )


def _next_calendar_date(value: date) -> date:
    return _next_weekday_date(value)


def _next_valid_fill_date(
    *,
    symbol: str,
    signal_time: str,
    dataset: MarketDataset | None,
) -> date:
    signal_dt = datetime.fromisoformat(signal_time)
    if dataset is not None:
        for bar in dataset.bars_by_symbol.get(symbol, ()):
            if bar.timestamp > signal_dt:
                return bar.timestamp.date()
    return _next_weekday_date(signal_dt.date())


def _next_weekday_date(value: date) -> date:
    next_date = value + timedelta(days=1)
    while next_date.weekday() >= 5:
        next_date += timedelta(days=1)
    return next_date


def _load_picks(paths: PaperOpsPaths, mode: PaperRunMode, run_date: date) -> tuple[PaperPick, ...]:
    payload = read_json(paths.exports / f"picks_{mode.value}_{run_date.isoformat()}.json", [])
    assert isinstance(payload, list)
    return tuple(_pick_from_row(row) for row in payload if isinstance(row, dict))


def _pick_from_row(row: dict[str, object]) -> PaperPick:
    return PaperPick(
        pick_id=str(row["pick_id"]),
        run_id=str(row["run_id"]),
        mode=PaperRunMode(str(row["mode"])),
        trade_date=str(row["trade_date"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=str(row["strategy_version"]),
        strategy_status=str(row["strategy_status"]),
        symbol=str(row["symbol"]),
        signal_time=str(row["signal_time"]),
        direction=str(row["direction"]),
        setup_score=float(row["setup_score"]),
        entry_reference=float(row["entry_reference"]),
        stop=_optional_float(row.get("stop")),
        target=_optional_float(row.get("target")),
        risk_per_unit=_optional_float(row.get("risk_per_unit")),
        reward_per_unit=_optional_float(row.get("reward_per_unit")),
        reward_risk=_optional_float(row.get("reward_risk")),
        decision=PaperPickDecision(str(row["decision"])),
        reason=str(row["reason"]),
        evidence=tuple(str(item) for item in row.get("evidence", [])),
        warnings=tuple(str(item) for item in row.get("warnings", [])),
    )


def _fill_order(
    order: PaperOrder,
    bar: MarketBar,
    run: PaperRun,
    config: PaperOpsConfig,
) -> PaperFill:
    rate = config.slippage_bps / 10_000.0
    fill_price = (
        bar.open * (1 + rate) if order.direction == Direction.LONG else bar.open * (1 - rate)
    )
    notional = fill_price * order.quantity
    return PaperFill(
        fill_id=stable_id("fill", order.order_id, bar.timestamp.isoformat()),
        order_id=order.order_id,
        run_id=run.run_id,
        mode=run.mode,
        strategy_id=order.strategy_id,
        symbol=order.symbol,
        fill_time=bar.timestamp.isoformat(),
        fill_price=fill_price,
        quantity=order.quantity,
        fee=notional * config.fee_bps / 10_000.0,
        slippage=abs(fill_price - bar.open) * order.quantity,
    )


def _position_from_fill(order: PaperOrder, fill: PaperFill) -> PaperPosition:
    return PaperPosition(
        position_id=stable_id("position", order.order_id),
        order_id=order.order_id,
        strategy_id=order.strategy_id,
        strategy_version=order.strategy_version,
        symbol=order.symbol,
        direction=order.direction,
        status=PaperPositionStatus.OPEN,
        opened_at=fill.fill_time,
        quantity=fill.quantity,
        entry_price=fill.fill_price,
        stop=order.stop,
        target=order.target,
        last_mark_price=fill.fill_price,
    )


def _check_position(
    position: PaperPosition,
    bar: MarketBar,
    run: PaperRun,
    config: PaperOpsConfig,
) -> tuple[PaperPosition, PaperClose | None]:
    opened_at = datetime.fromisoformat(position.opened_at)
    if (bar.timestamp.date() - opened_at.date()).days >= PAPER_TIMEOUT_DAYS:
        return position, _close_position(
            position, bar.close, PaperCloseReason.TIMEOUT, bar, run, config
        )
    stop_hit = (
        bar.low <= position.stop
        if position.direction == Direction.LONG
        else bar.high >= position.stop
    )
    target_hit = False
    if position.target is not None:
        target_hit = (
            bar.high >= position.target
            if position.direction == Direction.LONG
            else bar.low <= position.target
        )
    if stop_hit:
        return position, _close_position(
            position, position.stop, PaperCloseReason.STOP, bar, run, config
        )
    if target_hit and position.target is not None:
        return (
            position,
            _close_position(position, position.target, PaperCloseReason.TARGET, bar, run, config),
        )
    unrealized = _pnl(position.direction, position.entry_price, bar.close, position.quantity)
    checked = PaperPosition(
        position_id=position.position_id,
        order_id=position.order_id,
        strategy_id=position.strategy_id,
        strategy_version=position.strategy_version,
        symbol=position.symbol,
        direction=position.direction,
        status=PaperPositionStatus.OPEN,
        opened_at=position.opened_at,
        quantity=position.quantity,
        entry_price=position.entry_price,
        stop=position.stop,
        target=position.target,
        last_mark_price=bar.close,
        realized_pnl=position.realized_pnl,
        unrealized_pnl=unrealized,
    )
    return checked, None


def _close_position(
    position: PaperPosition,
    raw_price: float,
    reason: PaperCloseReason,
    bar: MarketBar,
    run: PaperRun,
    config: PaperOpsConfig,
) -> PaperClose:
    rate = config.slippage_bps / 10_000.0
    close_price = (
        raw_price * (1 - rate) if position.direction == Direction.LONG else raw_price * (1 + rate)
    )
    gross = _pnl(position.direction, position.entry_price, close_price, position.quantity)
    fee = close_price * position.quantity * config.fee_bps / 10_000.0
    slippage = abs(close_price - raw_price) * position.quantity
    risk_amount = abs(position.entry_price - position.stop) * position.quantity
    net = gross - fee
    return PaperClose(
        close_id=stable_id("close", position.position_id, bar.timestamp.isoformat(), reason.value),
        position_id=position.position_id,
        run_id=run.run_id,
        mode=run.mode,
        strategy_id=position.strategy_id,
        symbol=position.symbol,
        close_time=bar.timestamp.isoformat(),
        close_price=close_price,
        close_reason=reason,
        gross_pnl=gross,
        net_pnl=net,
        r_multiple=net / risk_amount if risk_amount else 0.0,
        fee=fee,
        slippage=slippage,
    )


def _pnl(direction: str, entry: float, exit_price: float, quantity: int) -> float:
    if direction == Direction.LONG:
        return (exit_price - entry) * quantity
    return (entry - exit_price) * quantity


def _next_bar_after(
    dataset: MarketDataset,
    symbol: str,
    signal_time: str,
    run_date: date,
) -> MarketBar | None:
    signal_dt = datetime.fromisoformat(signal_time)
    for bar in dataset.bars_by_symbol.get(symbol, ()):
        if bar.timestamp > signal_dt and bar.timestamp.date() <= run_date:
            return bar
    return None


def _latest_bar_on_or_before(
    dataset: MarketDataset, symbol: str, run_date: date
) -> MarketBar | None:
    bars = [
        bar for bar in dataset.bars_by_symbol.get(symbol, ()) if bar.timestamp.date() <= run_date
    ]
    return bars[-1] if bars else None


def _apply_close(
    accounts: dict[str, StrategyPaperAccount],
    close_record: PaperClose,
) -> dict[str, StrategyPaperAccount]:
    account = accounts[close_record.strategy_id]
    accounts[close_record.strategy_id] = StrategyPaperAccount(
        strategy_id=account.strategy_id,
        strategy_version=account.strategy_version,
        starting_equity=account.starting_equity,
        current_equity=account.current_equity + close_record.net_pnl,
        realized_pnl=account.realized_pnl + close_record.net_pnl,
        unrealized_pnl=0.0,
    )
    return accounts


def _apply_mark(
    accounts: dict[str, StrategyPaperAccount],
    position: PaperPosition,
) -> dict[str, StrategyPaperAccount]:
    account = accounts[position.strategy_id]
    accounts[position.strategy_id] = StrategyPaperAccount(
        strategy_id=account.strategy_id,
        strategy_version=account.strategy_version,
        starting_equity=account.starting_equity,
        current_equity=account.starting_equity + account.realized_pnl + position.unrealized_pnl,
        realized_pnl=account.realized_pnl,
        unrealized_pnl=position.unrealized_pnl,
    )
    return accounts


def _recalculate_unrealized_accounts(
    accounts: dict[str, StrategyPaperAccount],
    position_rows: list[dict[str, object]],
) -> dict[str, StrategyPaperAccount]:
    unrealized_by_strategy: dict[str, float] = {}
    for row in position_rows:
        strategy_id = str(row.get("strategy_id", ""))
        if not strategy_id:
            continue
        unrealized_by_strategy[strategy_id] = unrealized_by_strategy.get(strategy_id, 0.0) + float(
            row.get("unrealized_pnl", 0.0)
        )
    recalculated: dict[str, StrategyPaperAccount] = {}
    for strategy_id, account in accounts.items():
        unrealized = unrealized_by_strategy.get(strategy_id, 0.0)
        recalculated[strategy_id] = StrategyPaperAccount(
            strategy_id=account.strategy_id,
            strategy_version=account.strategy_version,
            starting_equity=account.starting_equity,
            current_equity=account.starting_equity + account.realized_pnl + unrealized,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=unrealized,
        )
    return recalculated


def _write_calendar_for_date(
    paths: PaperOpsPaths,
    run: PaperRun,
    manifest: DataTruthManifest,
    warnings: tuple[str, ...],
) -> None:
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    accounts = _accounts(paths, run.mode)
    strategy_rows = _strategy_registry(paths)
    position_rows = _state_rows(_open_positions_path(paths, run.mode), run.mode)
    pending_rows = _state_rows(_pending_orders_path(paths, run.mode), run.mode)
    rows: list[dict[str, object]] = []
    for strategy in strategy_rows:
        strategy_id = str(strategy["strategy_id"])
        day_events = [
            event
            for event in events
            if event.get("trade_date") == run.run_date and event.get("strategy_id") == strategy_id
        ]
        closes = [
            event
            for event in day_events
            if event.get("event_type") == "paper_position_closed"
            and isinstance(event.get("payload"), dict)
        ]
        fills = [
            event
            for event in day_events
            if event.get("event_type") == "paper_fill" and isinstance(event.get("payload"), dict)
        ]
        realized = sum(float(event["payload"].get("net_pnl", 0.0)) for event in closes)
        fees = sum(float(event["payload"].get("fee", 0.0)) for event in closes + fills)
        slippage = sum(float(event["payload"].get("slippage", 0.0)) for event in closes + fills)
        r_values = [float(event["payload"].get("r_multiple", 0.0)) for event in closes]
        account = accounts[strategy_id]
        open_count = sum(1 for row in position_rows if row.get("strategy_id") == strategy_id)
        pending_count = sum(1 for row in pending_rows if row.get("strategy_id") == strategy_id)
        unrealized = account.unrealized_pnl
        total_pnl = realized + unrealized
        ending = account.current_equity
        row = StrategyCalendarRow(
            date=run.run_date,
            mode=run.mode,
            strategy_id=strategy_id,
            strategy_version=str(strategy["strategy_version"]),
            strategy_status=str(strategy["strategy_status"]),
            data_snapshot_id=manifest.snapshot_id,
            starting_equity=account.starting_equity,
            ending_equity=ending,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=total_pnl,
            daily_return_pct=total_pnl / account.starting_equity,
            cumulative_return_pct=(ending - account.starting_equity) / account.starting_equity,
            drawdown_pct=min(0.0, (ending - account.starting_equity) / account.starting_equity),
            trades_opened=len(fills),
            trades_closed=len(closes),
            pending_orders=pending_count,
            open_positions=open_count,
            wins=sum(1 for event in closes if float(event["payload"].get("net_pnl", 0.0)) > 0),
            losses=sum(1 for event in closes if float(event["payload"].get("net_pnl", 0.0)) < 0),
            flats=sum(1 for event in closes if float(event["payload"].get("net_pnl", 0.0)) == 0),
            average_r=sum(r_values) / len(r_values) if r_values else 0.0,
            expectancy_r=sum(r_values) / len(r_values) if r_values else 0.0,
            exposure_pct=0.0,
            fees_paid=fees,
            slippage_estimate=slippage,
            warnings=warnings,
            run_id=run.run_id,
        )
        rows.append(row.to_dict())
    fieldnames = (
        "date",
        "mode",
        "strategy_id",
        "strategy_version",
        "strategy_status",
        "data_snapshot_id",
        "starting_equity",
        "ending_equity",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
        "daily_return_pct",
        "cumulative_return_pct",
        "drawdown_pct",
        "trades_opened",
        "trades_closed",
        "pending_orders",
        "open_positions",
        "wins",
        "losses",
        "flats",
        "average_r",
        "expectancy_r",
        "exposure_pct",
        "fees_paid",
        "slippage_estimate",
        "warnings",
        "run_id",
    )
    upsert_rows(
        paths.calendar / "strategy_daily_returns.csv",
        rows,
        ("date", "mode", "strategy_id"),
        fieldnames,
    )
    all_rows = _read_calendar_rows(paths)
    write_json(paths.calendar / "strategy_daily_returns.json", all_rows if all_rows else rows)


def _read_calendar_rows(paths: PaperOpsPaths) -> list[dict[str, object]]:
    csv_path = paths.calendar / "strategy_daily_returns.csv"
    if not csv_path.exists():
        return []
    import csv

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_calendar_matrix(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    dates = sorted({str(row["date"]) for row in rows})
    strategies = sorted({str(row["strategy_id"]) for row in rows})
    matrix_rows: list[dict[str, object]] = []
    for row_date in dates:
        row: dict[str, object] = {"date": row_date}
        aggregate = 0.0
        count = 0
        for strategy in strategies:
            match = next(
                (
                    item
                    for item in rows
                    if item["date"] == row_date and item["strategy_id"] == strategy
                ),
                None,
            )
            value = float(match["daily_return_pct"]) if match else 0.0
            row[strategy] = value
            aggregate += value
            count += 1
        row["aggregate"] = aggregate / count if count else 0.0
        matrix_rows.append(row)
    write_csv(
        paths.calendar / "strategy_calendar_matrix.csv",
        matrix_rows,
        ("date", *tuple(strategies), "aggregate"),
    )


def _write_monthly_returns(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    output: list[dict[str, object]] = []
    for key in sorted({(str(row["date"])[:7], str(row["strategy_id"])) for row in rows}):
        month, strategy_id = key
        matches = [
            row
            for row in rows
            if str(row["date"]).startswith(month) and row["strategy_id"] == strategy_id
        ]
        monthly_return = sum(float(row["daily_return_pct"]) for row in matches)
        output.append(
            {
                "month": month,
                "strategy_id": strategy_id,
                "monthly_return_pct": monthly_return,
                "cumulative_return_pct": matches[-1]["cumulative_return_pct"] if matches else 0,
                "win_days": sum(1 for row in matches if float(row["daily_return_pct"]) > 0),
                "loss_days": sum(1 for row in matches if float(row["daily_return_pct"]) < 0),
                "flat_days": sum(1 for row in matches if float(row["daily_return_pct"]) == 0),
                "max_drawdown_pct": min(float(row["drawdown_pct"]) for row in matches)
                if matches
                else 0,
            }
        )
    write_csv(
        paths.calendar / "strategy_monthly_returns.csv",
        output,
        (
            "month",
            "strategy_id",
            "monthly_return_pct",
            "cumulative_return_pct",
            "win_days",
            "loss_days",
            "flat_days",
            "max_drawdown_pct",
        ),
    )


def _write_equity_and_drawdown(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    equity_rows = [
        {
            "date": row["date"],
            "strategy_id": row["strategy_id"],
            "equity": row["ending_equity"],
            "cumulative_return_pct": row["cumulative_return_pct"],
        }
        for row in rows
    ]
    drawdown_rows = [
        {
            "date": row["date"],
            "strategy_id": row["strategy_id"],
            "equity": row["ending_equity"],
            "peak_equity": row["starting_equity"],
            "drawdown_pct": row["drawdown_pct"],
        }
        for row in rows
    ]
    write_csv(
        paths.calendar / "strategy_equity_curves.csv",
        equity_rows,
        ("date", "strategy_id", "equity", "cumulative_return_pct"),
    )
    write_csv(
        paths.calendar / "strategy_drawdowns.csv",
        drawdown_rows,
        ("date", "strategy_id", "equity", "peak_equity", "drawdown_pct"),
    )


def _write_calendar_summary(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    if not rows:
        summary = "# PaperOps Calendar Summary\n\nNo calendar rows yet.\n"
    else:
        latest_date = max(str(row["date"]) for row in rows)
        latest = [row for row in rows if row["date"] == latest_date]
        best = max(latest, key=lambda row: float(row["daily_return_pct"]))
        worst = min(latest, key=lambda row: float(row["daily_return_pct"]))
        drawdown = [row["strategy_id"] for row in latest if float(row["drawdown_pct"]) < 0]
        summary = "\n".join(
            [
                "# PaperOps Calendar Summary",
                "",
                f"- Latest date: `{latest_date}`",
                f"- Best strategy today: `{best['strategy_id']}`",
                f"- Worst strategy today: `{worst['strategy_id']}`",
                f"- Strategies in drawdown: {', '.join(str(item) for item in drawdown) or 'none'}",
                "- Improving strategies: n/a until more forward days exist.",
                "- Decaying strategies: n/a until more forward days exist.",
                "- Overtrading strategies: none flagged by v1 limits.",
                "- Watch: strategies with negative cumulative return or severe drawdown.",
                "- Quarantine: no automatic quarantine in v1.",
                "- Forward paper days: "
                f"`{len({r['date'] for r in rows if r['mode'] == 'forward'})}`",
                "- Replay evidence days: "
                f"`{len({r['date'] for r in rows if r['mode'] == 'replay'})}`",
            ]
        )
    (paths.calendar / "calendar_summary.md").write_text(summary + "\n", encoding="utf-8")


def _write_daily_report(
    paths: PaperOpsPaths,
    run: PaperRun,
    manifest: DataTruthManifest,
    stats: dict[str, object],
    warnings: tuple[str, ...],
) -> None:
    payload = {
        "data_snapshot_id": manifest.snapshot_id,
        "date": run.run_date,
        "mode": run.mode.value,
        "provider_status": manifest.validation_status,
        "reconciliation_status": "single_provider_unreconciled",
        "run_id": run.run_id,
        "stats": stats,
        "warnings": list(warnings),
    }
    write_json(paths.reports / "daily" / f"{run.run_date}.json", payload)
    lines = [
        f"# PaperOps Daily Report {run.run_date}",
        "",
        f"- Mode: `{run.mode.value}`",
        f"- Run ID: `{run.run_id}`",
        f"- Data snapshot: `{manifest.snapshot_id}`",
        f"- Provider/reconciliation: `{payload['reconciliation_status']}`",
        f"- Stats: `{json.dumps(stats, sort_keys=True)}`",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in warnings)
    (paths.reports / "daily" / f"{run.run_date}.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _write_reconciliation_markdown(
    paths: PaperOpsPaths,
    report_payload: PaperOpsReconciliationReport,
) -> None:
    lines = [
        "# PaperOps Reconciliation",
        "",
        f"- Status: `{report_payload.status}`",
        f"- Duplicate events: `{len(report_payload.duplicate_event_ids)}`",
        f"- Orphan fills: `{len(report_payload.orphan_fills)}`",
        f"- Orphan closes: `{len(report_payload.orphan_closes)}`",
    ]
    (paths.reconciliation / "reconciliation_latest.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _write_manifest(
    paths: PaperOpsPaths,
    run: PaperRun,
    artifacts: tuple[Path, ...],
    warnings: tuple[str, ...],
) -> None:
    manifest = PaperOpsManifest(
        run_id=run.run_id,
        mode=run.mode,
        run_date=run.run_date,
        data_snapshot_id=run.data_snapshot_id,
        output_artifacts=tuple(path.as_posix() for path in artifacts),
        warnings=warnings,
    )
    write_json(paths.manifests / f"{_safe_filename(run.run_id)}.json", manifest.to_dict())


def _append_events(paths: PaperOpsPaths, events: list[PaperLedgerEvent]) -> None:
    append_jsonl_unique(
        paths.ledger / "paper_ledger.jsonl",
        [event.to_dict() for event in events],
        "event_id",
    )


def _safe_filename(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def _event(
    run: PaperRun,
    phase: PaperJobPhase,
    strategy_id: str | None,
    symbol: str | None,
    event_type: str,
    entity_id: str,
    payload: dict[str, object],
) -> PaperLedgerEvent:
    return PaperLedgerEvent(
        event_id=stable_id(run.run_id, phase.value, event_type, entity_id),
        event_type=event_type,
        run_id=run.run_id,
        mode=run.mode,
        trade_date=run.run_date,
        strategy_id=strategy_id,
        symbol=symbol,
        payload=payload,
    )


def _config(paths: PaperOpsPaths) -> PaperOpsConfig:
    payload = read_json(paths.state / "paper_ops_config.json", {})
    if not isinstance(payload, dict) or not payload:
        init(output_root=paths.root)
        payload = read_json(paths.state / "paper_ops_config.json", {})
    assert isinstance(payload, dict)
    return PaperOpsConfig(
        starting_equity=float(payload.get("starting_equity", 100_000.0)),
        risk_per_trade_pct=float(payload.get("risk_per_trade_pct", 0.005)),
        max_daily_loss_pct=float(payload.get("max_daily_loss_pct", 0.015)),
        max_open_risk_pct=float(payload.get("max_open_risk_pct", 0.02)),
        max_concurrent_positions=int(payload.get("max_concurrent_positions", 3)),
        allow_experimental=bool(payload.get("allow_experimental", True)),
        allow_single_provider_forward=bool(payload.get("allow_single_provider_forward", True)),
        min_reward_risk=float(payload.get("min_reward_risk", 1.0)),
        fee_bps=float(payload.get("fee_bps", 1.0)),
        slippage_bps=float(payload.get("slippage_bps", 5.0)),
    )


def _strategy_registry(paths: PaperOpsPaths) -> list[dict[str, object]]:
    payload = read_json(paths.state / "strategy_registry.json", [])
    assert isinstance(payload, list)
    return [row for row in payload if isinstance(row, dict)]


def _pending_orders_path(paths: PaperOpsPaths, mode: PaperRunMode) -> Path:
    if mode is PaperRunMode.FORWARD:
        return paths.state / "pending_orders.json"
    return paths.state / f"{mode.value}_pending_orders.json"


def _open_positions_path(paths: PaperOpsPaths, mode: PaperRunMode) -> Path:
    if mode is PaperRunMode.FORWARD:
        return paths.state / "open_positions.json"
    return paths.state / f"{mode.value}_open_positions.json"


def _paper_accounts_path(paths: PaperOpsPaths, mode: PaperRunMode) -> Path:
    if mode is PaperRunMode.FORWARD:
        return paths.state / "paper_accounts.json"
    return paths.state / f"{mode.value}_paper_accounts.json"


def _fresh_account_payload(
    paths: PaperOpsPaths, config: PaperOpsConfig | None = None
) -> dict[str, object]:
    active_config = config or _config(paths)
    accounts = [
        StrategyPaperAccount(
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            starting_equity=active_config.starting_equity,
            current_equity=active_config.starting_equity,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
        ).to_dict()
        for row in _strategy_registry(paths)
    ]
    return {"accounts": accounts, "schema_version": "v2.paper_account_state.v1"}


def _accounts(
    paths: PaperOpsPaths,
    mode: PaperRunMode = PaperRunMode.FORWARD,
) -> dict[str, StrategyPaperAccount]:
    account_path = _paper_accounts_path(paths, mode)
    payload = read_json(account_path, {})
    if not isinstance(payload, dict) or not payload.get("accounts"):
        payload = _fresh_account_payload(paths)
        write_json(account_path, payload)
    assert isinstance(payload, dict)
    rows = payload.get("accounts", [])
    assert isinstance(rows, list)
    accounts: dict[str, StrategyPaperAccount] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        account = StrategyPaperAccount(
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            starting_equity=float(row["starting_equity"]),
            current_equity=float(row["current_equity"]),
            realized_pnl=float(row["realized_pnl"]),
            unrealized_pnl=float(row["unrealized_pnl"]),
        )
        accounts[account.strategy_id] = account
    return accounts


def _write_accounts(
    paths: PaperOpsPaths,
    mode: PaperRunMode,
    accounts: dict[str, StrategyPaperAccount],
) -> None:
    state = PaperAccountState(accounts=tuple(accounts.values()))
    write_json(_paper_accounts_path(paths, mode), state.to_dict())


def _dict_list(path: Path) -> list[dict[str, object]]:
    payload = read_json(path, [])
    assert isinstance(payload, list)
    return [row for row in payload if isinstance(row, dict)]


def _state_rows(path: Path, mode: PaperRunMode) -> list[dict[str, object]]:
    rows = _dict_list(path)
    return [row for row in rows if _infer_row_mode(row) is mode]


def _repair_pending_order_rows(
    rows: list[dict[str, object]],
    dataset: MarketDataset,
) -> list[dict[str, object]]:
    repaired_rows: list[dict[str, object]] = []
    for row in rows:
        repaired = dict(row)
        symbol = str(repaired.get("symbol", ""))
        signal_time = str(repaired.get("signal_time", ""))
        if symbol and signal_time:
            expected_fill = _next_valid_fill_date(
                symbol=symbol,
                signal_time=signal_time,
                dataset=dataset,
            ).isoformat()
            if repaired.get("earliest_fill_date") != expected_fill:
                repaired["earliest_fill_date"] = expected_fill
                warnings = [str(item) for item in repaired.get("warnings", [])]
                warnings.append(
                    "earliest_fill_date repaired from legacy calendar-day logic"
                )
                repaired["warnings"] = list(dict.fromkeys(warnings))
        repaired_rows.append(repaired)
    return repaired_rows


def _infer_row_mode(row: dict[str, object]) -> PaperRunMode:
    explicit_mode = row.get("mode")
    if explicit_mode in {item.value for item in PaperRunMode}:
        return PaperRunMode(str(explicit_mode))
    joined_ids = " ".join(
        str(row.get(key, "")) for key in ("order_id", "pick_id", "position_id", "run_id")
    )
    for mode in PaperRunMode:
        if f":{mode.value}:" in joined_ids or joined_ids.startswith(f"{mode.value}:"):
            return mode
    return PaperRunMode.FORWARD


def _repair_legacy_default_state(paths: PaperOpsPaths, config: PaperOpsConfig) -> None:
    pending_path = paths.state / "pending_orders.json"
    positions_path = paths.state / "open_positions.json"
    pending_rows = _dict_list(pending_path)
    position_rows = _dict_list(positions_path)
    pending_by_mode = _rows_by_mode(pending_rows)
    positions_by_mode = _rows_by_mode(position_rows)
    forward_pending = _dedupe_generated_rows(pending_by_mode[PaperRunMode.FORWARD], "order_id")
    forward_positions = _dedupe_generated_rows(
        positions_by_mode[PaperRunMode.FORWARD],
        "position_id",
    )
    if len(forward_pending) != len(pending_rows):
        write_json(pending_path, forward_pending)
    if len(forward_positions) != len(position_rows):
        write_json(positions_path, forward_positions)
    for mode in (PaperRunMode.REPLAY, PaperRunMode.DEMO):
        _merge_generated_state_rows(
            _pending_orders_path(paths, mode),
            pending_by_mode[mode],
            "order_id",
        )
        _merge_generated_state_rows(
            _open_positions_path(paths, mode),
            positions_by_mode[mode],
            "position_id",
        )
        _dedupe_generated_state_file(_pending_orders_path(paths, mode), "order_id")
        _dedupe_generated_state_file(_open_positions_path(paths, mode), "position_id")
    if forward_positions:
        return
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    has_forward_fill_or_close = any(
        event.get("mode") == PaperRunMode.FORWARD.value
        and event.get("event_type") in {"paper_fill", "paper_position_closed"}
        for event in events
    )
    if not has_forward_fill_or_close:
        write_json(paths.state / "paper_accounts.json", _fresh_account_payload(paths, config))


def _reset_mode_generated_state(
    paths: PaperOpsPaths,
    mode: PaperRunMode,
    config: PaperOpsConfig,
) -> None:
    if mode is PaperRunMode.FORWARD:
        raise ValueError("forward PaperOps state must not be reset by replay repair")
    write_json(_pending_orders_path(paths, mode), [])
    write_json(_open_positions_path(paths, mode), [])
    write_json(_paper_accounts_path(paths, mode), _fresh_account_payload(paths, config))


def _rows_by_mode(rows: list[dict[str, object]]) -> dict[PaperRunMode, list[dict[str, object]]]:
    by_mode = {mode: [] for mode in PaperRunMode}
    for row in rows:
        by_mode[_infer_row_mode(row)].append(row)
    return by_mode


def _merge_generated_state_rows(
    path: Path,
    rows: list[dict[str, object]],
    id_field: str,
) -> None:
    if not rows:
        return
    existing_rows = _dict_list(path)
    write_json(path, _dedupe_generated_rows(existing_rows + rows, id_field))


def _dedupe_generated_state_file(path: Path, id_field: str) -> None:
    rows = _dict_list(path)
    if rows:
        write_json(path, _dedupe_generated_rows(rows, id_field))


def _dedupe_generated_rows(
    rows: list[dict[str, object]],
    id_field: str,
) -> list[dict[str, object]]:
    by_id = {str(row.get(id_field)): row for row in rows if row.get(id_field)}
    return [by_id[key] for key in sorted(by_id)]


def _order_from_row(row: dict[str, object]) -> PaperOrder:
    return PaperOrder(
        order_id=str(row["order_id"]),
        pick_id=str(row["pick_id"]),
        run_id=str(row["run_id"]),
        mode=PaperRunMode(str(row["mode"])),
        trade_date=str(row["trade_date"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=str(row["strategy_version"]),
        symbol=str(row["symbol"]),
        direction=str(row["direction"]),
        order_status=PaperOrderStatus(str(row["order_status"])),
        expected_fill_rule=str(row["expected_fill_rule"]),
        signal_time=str(row["signal_time"]),
        earliest_fill_date=str(row["earliest_fill_date"]),
        entry=float(row["entry"]),
        stop=float(row["stop"]),
        target=_optional_float(row.get("target")),
        risk_per_unit=float(row["risk_per_unit"]),
        reward_per_unit=_optional_float(row.get("reward_per_unit")),
        reward_risk=_optional_float(row.get("reward_risk")),
        risk_budget=float(row["risk_budget"]),
        quantity=int(row["quantity"]),
        notional_exposure=float(row["notional_exposure"]),
        max_loss_estimate=float(row["max_loss_estimate"]),
        strategy_equity_basis=float(row["strategy_equity_basis"]),
        warnings=tuple(str(item) for item in row.get("warnings", [])),
    )


def _position_from_row(row: dict[str, object]) -> PaperPosition:
    return PaperPosition(
        position_id=str(row["position_id"]),
        order_id=str(row["order_id"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=str(row["strategy_version"]),
        symbol=str(row["symbol"]),
        direction=str(row["direction"]),
        status=PaperPositionStatus(str(row["status"])),
        opened_at=str(row["opened_at"]),
        quantity=int(row["quantity"]),
        entry_price=float(row["entry_price"]),
        stop=float(row["stop"]),
        target=_optional_float(row.get("target")),
        last_mark_price=float(row["last_mark_price"]),
        realized_pnl=float(row.get("realized_pnl", 0.0)),
        unrealized_pnl=float(row.get("unrealized_pnl", 0.0)),
    )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
