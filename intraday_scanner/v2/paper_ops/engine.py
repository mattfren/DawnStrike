"""Scheduler-ready PaperOps v1 file-backed engine."""

# mypy: ignore-errors

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from intraday_scanner.market_calendar import (
    FIRST_ELIGIBLE_ACTIVATION_POLICY,
    MARKET_TIMEZONE,
    NEXT_SESSION_ACTIVATION_POLICY,
    market_session,
    next_session_after_registration,
    registration_coverage_inception_date,
)
from intraday_scanner.v2.backtest import BacktestResult
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.data.synthetic import build_synthetic_ohlcv_dataset
from intraday_scanner.v2.data_truth import build_data_truth_snapshot
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.paper_ops.experiment_registry import (
    write_experiment_registry,
)
from intraday_scanner.v2.paper_ops.models import (
    DEFAULT_PAPEROPS_UNIVERSE,
    PAPER_EXECUTION_POLICY_VERSION,
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
    exclusive_file_lock,
    jsonl_lock_path,
    read_json,
    read_jsonl,
    upsert_rows,
    write_csv,
    write_json,
    write_jsonl,
)
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.scanner import ScanCard, ScanOutput, run_latest_scan
from intraday_scanner.v2.strategies import Direction, StrategySpec, build_strategy_catalog
from intraday_scanner.v2.strategy_identity import (
    strategy_semantics_fingerprint,
    strategy_semantics_payload,
)

PAPER_TIMEOUT_DAYS = 10
_ACTIVATION_POLICY_FIRST_ELIGIBLE = FIRST_ELIGIBLE_ACTIVATION_POLICY
_ACTIVATION_POLICY_NEXT_SESSION = NEXT_SESSION_ACTIVATION_POLICY
CALENDAR_FIELDNAMES = (
    "date",
    "mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
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
_DATASET_CACHE: dict[
    tuple[str, str, bool, tuple[str, ...], bool, str],
    tuple[MarketDataset, DataTruthManifest, tuple[str, ...]],
] = {}
_RECONCILIATION_STATUS_BY_SNAPSHOT: dict[str, str] = {}


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
    def resolve(cls, root: Path) -> PaperOpsPaths:
        """Construct canonical paths without touching the filesystem."""

        return cls(
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

    @classmethod
    def create(cls, root: Path) -> PaperOpsPaths:
        paths = cls.resolve(root)
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
    _recover_pending_transaction(paths)
    config_path = paths.state / "paper_ops_config.json"
    stored_config = read_json(config_path, {})
    config = (
        _config_from_payload(stored_config)
        if isinstance(stored_config, dict) and stored_config
        else PaperOpsConfig()
    )
    _ensure_execution_policy_manifest(paths, config)
    write_json(config_path, config.to_dict())
    strategies = tuple(build_strategy_catalog())
    semantic_fingerprints = _ensure_strategy_semantics_manifest(paths, strategies)
    registry = [
        {
            **config_row.to_dict(),
            "strategy_semantics_fingerprint": semantic_fingerprints[
                (config_row.strategy_id, config_row.strategy_version)
            ],
        }
        for config_row in _strategy_configs(config, strategies)
    ]
    stored_registry = read_json(paths.state / "strategy_registry.json", [])
    _assert_strategy_registry_upgrade_safe(paths, stored_registry, registry)
    write_json(paths.state / "strategy_registry.json", registry)
    experiment_registry_path = paths.state / "experiment_registry.json"
    if not experiment_registry_path.exists():
        write_experiment_registry(experiment_registry_path)
    if not (paths.state / "pending_orders.json").exists():
        write_json(paths.state / "pending_orders.json", [])
    if not (paths.state / "open_positions.json").exists():
        write_json(paths.state / "open_positions.json", [])
    if not (paths.state / "paper_accounts.json").exists():
        write_json(paths.state / "paper_accounts.json", _fresh_account_payload(paths, config))
    _repair_legacy_default_state(paths, config)
    accounts_by_mode = {mode.value: _accounts(paths, mode) for mode in PaperRunMode}
    return {
        "status": "initialized",
        "output_root": output_root.as_posix(),
        "strategy_account_count": len(accounts_by_mode[PaperRunMode.FORWARD.value]),
        "strategy_account_counts_by_mode": {
            mode: len(accounts) for mode, accounts in accounts_by_mode.items()
        },
    }


def preflight(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = PaperOpsPaths.create(output_root)
    _recover_pending_transaction(paths)
    config = _config(paths)
    data_truth_root = _data_truth_root_for_mode(paths, mode)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
        universe_symbols=config.universe_symbols,
        allow_single_provider_forward=config.allow_single_provider_forward,
        data_truth_root=data_truth_root,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    _ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=manifest,
        data_truth_root=data_truth_root,
    )
    payload = {
        "data_snapshot_id": manifest.snapshot_id,
        "latest_completed_date": manifest.accepted_end,
        "reconciliation_status": _reconciliation_status(manifest),
        "mode": mode.value,
        "run_date": run_date.isoformat(),
        "run_id": run.run_id,
        "status": "passed_with_warnings" if warnings else "passed",
        "symbols": list(dataset.symbols),
        "universe_id": config.universe_id,
        "universe_symbols": list(config.universe_symbols),
        "universe_status": "complete",
        "warnings": list(warnings),
    }
    write_json(paths.exports / f"preflight_{mode.value}_{run_date.isoformat()}.json", payload)
    return payload


def scan(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    _recover_pending_transaction(paths)
    config = _config(paths)
    data_truth_root = _data_truth_root_for_mode(paths, mode)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
        universe_symbols=config.universe_symbols,
        allow_single_provider_forward=config.allow_single_provider_forward,
        data_truth_root=data_truth_root,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    _ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=manifest,
        data_truth_root=data_truth_root,
    )
    strategies = _strategies_eligible_for_run(
        paths,
        config=config,
        run_date=run_date,
        mode=mode,
    )
    results = _backtest_results(dataset, strategies, config)
    scan_output = run_latest_scan(
        dataset,
        strategies,
        results,
        risk_settings=RiskSettings(
            account_equity=config.starting_equity,
            risk_per_trade_pct=config.risk_per_trade_pct,
            max_position_pct=config.max_gross_exposure_pct,
            min_reward_risk=config.min_reward_risk,
            max_risk_per_trade_pct=config.risk_per_trade_pct,
        ),
        data_snapshot_id=manifest.snapshot_id,
        run_manifest_id=run.run_id,
    )
    picks = _picks_from_scan(scan_output, strategies, run, config, warnings)
    semantics_by_id = {
        strategy.strategy_id: _strategy_semantics_fingerprint(strategy) for strategy in strategies
    }
    no_setup_decisions = [
        _no_setup_decision(
            card,
            run,
            config,
            warnings,
            semantics_by_id.get(card.strategy_id, "unknown"),
        )
        for card in scan_output.no_setup
    ]
    expected_decisions = len(strategies) * len(dataset.symbols)
    observed_decisions = len(picks) + len(no_setup_decisions)
    if observed_decisions != expected_decisions:
        raise ValueError(
            "PaperOps strategy decision coverage mismatch: "
            f"expected {expected_decisions}, observed {observed_decisions}"
        )
    picks_path = paths.exports / f"picks_{mode.value}_{run_date.isoformat()}.json"
    write_json(picks_path, [pick.to_dict() for pick in picks])
    write_json(
        paths.exports / f"strategy_decisions_{mode.value}_{run_date.isoformat()}.json",
        [
            *(
                {
                    **pick.to_dict(),
                    "decision_status": pick.decision.value,
                    "trade_return_eligible": pick.decision is PaperPickDecision.ACCEPTED,
                    "trade_return_pct": None,
                }
                for pick in picks
            ),
            *no_setup_decisions,
        ],
    )
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
        ]
        + [
            _event(
                run,
                PaperJobPhase.SCAN,
                str(row["strategy_id"]),
                str(row["symbol"]),
                "paper_no_setup_decision",
                str(row["decision_id"]),
                row,
            )
            for row in no_setup_decisions
        ],
    )
    accepted = [pick for pick in picks if pick.decision is PaperPickDecision.ACCEPTED]
    _write_daily_report(
        paths,
        run,
        manifest,
        {
            "accepted_picks": len(accepted),
            "decision_coverage": observed_decisions,
            "decision_coverage_status": "complete",
            "no_setup_decisions": len(no_setup_decisions),
            "phase": "scan",
            "picks": len(picks),
        },
        warnings,
    )
    return {
        "accepted_picks": len(accepted),
        "data_snapshot_id": manifest.snapshot_id,
        "decision_coverage": observed_decisions,
        "decision_coverage_status": "complete",
        "no_setup_decisions": len(no_setup_decisions),
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
    _recover_pending_transaction(paths)
    config = _config(paths)
    data_truth_root = _data_truth_root_for_mode(paths, mode)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
        universe_symbols=config.universe_symbols,
        allow_single_provider_forward=config.allow_single_provider_forward,
        data_truth_root=data_truth_root,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    _ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=manifest,
        data_truth_root=data_truth_root,
    )
    _repair_legacy_default_state(paths, config)
    picks = _load_picks(paths, mode, run_date)
    if not picks:
        scan(run_date=run_date, mode=mode, output_root=output_root, allow_fetch=allow_fetch)
        picks = _load_picks(paths, mode, run_date)
    _validate_picks_for_run(picks, run, config, paths)
    pending_path = _pending_orders_path(paths, mode)
    pending = _repair_pending_order_rows(_state_rows(pending_path, mode), dataset)
    position_rows = _state_rows(_open_positions_path(paths, mode), mode)
    accounts = _accounts(paths, mode)
    ledger_events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    existing_ids = {str(row.get("order_id")) for row in pending if row.get("order_id")} | {
        str(payload.get("order_id"))
        for event in ledger_events
        for payload in [event.get("payload")]
        if _event_matches_mode(event, mode)
        and event.get("event_type") == "paper_order_created"
        and isinstance(payload, dict)
        and payload.get("order_id")
    }
    daily_net = _daily_closed_net_by_strategy(ledger_events, run.run_date, mode)
    strategies_by_id = {strategy.strategy_id: strategy for strategy in build_strategy_catalog()}
    orders: list[PaperOrder] = []
    blocked: list[dict[str, object]] = []
    for pick in picks:
        if pick.decision is not PaperPickDecision.ACCEPTED:
            continue
        account = accounts[pick.strategy_id]
        order = _order_from_pick(
            pick,
            run,
            config,
            dataset,
            equity_basis=account.current_equity,
        )
        if order.order_id in existing_ids:
            continue
        strategy = strategies_by_id.get(order.strategy_id)
        if strategy is None or strategy.version != order.strategy_version:
            raise ValueError("PaperOps pick strategy is absent from the active exact registry")
        governance_reason = _governance_block_reason(paths, strategy, config)
        if governance_reason is not None:
            blocked.append(
                _blocked_order_payload(
                    order,
                    f"strategy_governance_pause:{governance_reason}",
                    run,
                )
            )
            continue
        reason = _order_entry_block_reason(
            order,
            position_rows=position_rows,
            pending_rows=pending + [item.to_dict() for item in orders],
            account=account,
            config=config,
            daily_closed_net=daily_net.get(_strategy_version_key(order), 0.0),
        )
        if reason is not None:
            blocked.append(_blocked_order_payload(order, reason, run))
            continue
        orders.append(order)
        existing_ids.add(order.order_id)
    pending.extend(order.to_dict() for order in orders)
    write_json(
        paths.exports / f"order_decisions_{mode.value}_{run_date.isoformat()}.json",
        [
            *(
                {
                    "decision": "created",
                    "reason": "risk_checks_passed",
                    **order.to_dict(),
                }
                for order in orders
            ),
            *blocked,
        ],
    )
    transaction_events = [
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
    ] + [
        _event(
            run,
            PaperJobPhase.ENTER,
            str(row["strategy_id"]),
            str(row["symbol"]),
            "paper_order_blocked",
            f"{row['order_id']}:{row['reason']}",
            row,
        )
        for row in blocked
    ]
    _commit_paper_transaction(
        paths,
        events=transaction_events,
        state_updates={pending_path: pending},
    )
    _write_daily_report(
        paths,
        run,
        manifest,
        {
            "orders_blocked": len(blocked),
            "orders_created": len(orders),
            "pending_orders": len(pending),
            "phase": "enter",
        },
        warnings,
    )
    return {
        "orders_blocked": len(blocked),
        "orders_created": len(orders),
        "pending_orders": len(pending),
        "run_id": run.run_id,
    }


def check(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    paths = PaperOpsPaths.create(output_root)
    _recover_pending_transaction(paths)
    config = _config(paths)
    data_truth_root = _data_truth_root_for_mode(paths, mode)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
        universe_symbols=config.universe_symbols,
        allow_single_provider_forward=config.allow_single_provider_forward,
        data_truth_root=data_truth_root,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    _ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=manifest,
        data_truth_root=data_truth_root,
    )
    _repair_legacy_default_state(paths, config)
    pending_path = _pending_orders_path(paths, mode)
    positions_path = _open_positions_path(paths, mode)
    pending_rows = _repair_pending_order_rows(_state_rows(pending_path, mode), dataset)
    position_rows = _state_rows(positions_path, mode)
    accounts = _accounts(paths, mode)
    terminal_orders: set[str] = set()
    new_positions: list[PaperPosition] = []
    fills: list[PaperFill] = []
    fill_source_bars: dict[str, MarketBar] = {}
    position_source_bars: dict[str, MarketBar] = {}
    close_source_bars: dict[str, MarketBar] = {}
    blocked_orders: list[dict[str, object]] = []
    pending_no_fill_events: list[PaperLedgerEvent] = []
    ledger_events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    daily_net = _daily_closed_net_by_strategy(ledger_events, run.run_date, mode)

    for order in [_order_from_row(row) for row in pending_rows]:
        fill_bar = _next_bar_after(dataset, order.symbol, order.signal_time, run_date)
        if fill_bar is None:
            pending_no_fill_events.append(
                _event(
                    run,
                    PaperJobPhase.CHECK,
                    order.strategy_id,
                    order.symbol,
                    "paper_order_pending_no_fill_data",
                    f"{order.order_id}:pending_check:{run_date}",
                    _pending_order_lifecycle_payload(order, run),
                )
            )
            continue
        if fill_bar.timestamp.date() < run_date:
            blocked_orders.append(
                _blocked_order_payload(
                    order,
                    "missed_fill_session",
                    run,
                    source_bar=fill_bar,
                )
            )
            terminal_orders.add(order.order_id)
            continue
        account = accounts[order.strategy_id]
        fill = _fill_order(order, fill_bar, run, config)
        position = _position_from_fill(order, fill)
        reason = _fill_entry_block_reason(
            order,
            fill=fill,
            position=position,
            fill_bar=fill_bar,
            position_rows=position_rows + [item.to_dict() for item in new_positions],
            pending_rows=[],
            account=account,
            config=config,
            daily_closed_net=daily_net.get(_strategy_version_key(order), 0.0),
        )
        if reason is not None:
            blocked_orders.append(_blocked_order_payload(order, reason, run, source_bar=fill_bar))
            terminal_orders.add(order.order_id)
            continue
        fills.append(fill)
        new_positions.append(position)
        fill_source_bars[fill.fill_id] = fill_bar
        position_source_bars[position.position_id] = fill_bar
        terminal_orders.add(order.order_id)
    remaining_pending = [
        row for row in pending_rows if str(row.get("order_id")) not in terminal_orders
    ]
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
            close_source_bars[close_record.close_id] = bar
            accounts = _apply_close(accounts, close_record)
        else:
            updated_positions.append(checked.to_dict())
            checked_no_action.append(checked)
            position_source_bars[checked.position_id] = bar
            accounts = _apply_mark(accounts, checked)

    accounts = _recalculate_unrealized_accounts(accounts, updated_positions)
    transaction_events = (
        [
            _event(
                run,
                PaperJobPhase.CHECK,
                fill.strategy_id,
                fill.symbol,
                "paper_fill",
                fill.fill_id,
                _with_source_bar(fill.to_dict(), fill_source_bars[fill.fill_id], run),
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
                _with_source_bar(
                    position.to_dict(),
                    position_source_bars[position.position_id],
                    run,
                ),
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
                _with_source_bar(
                    close_record.to_dict(),
                    close_source_bars[close_record.close_id],
                    run,
                ),
            )
            for close_record in closes
        ]
        + [
            _event(
                run,
                PaperJobPhase.CHECK,
                str(row["strategy_id"]),
                str(row["symbol"]),
                "paper_order_blocked",
                f"{row['order_id']}:{row['reason']}",
                row,
            )
            for row in blocked_orders
        ]
        + [
            _event(
                run,
                PaperJobPhase.CHECK,
                position.strategy_id,
                position.symbol,
                "paper_position_checked_no_action",
                f"{position.position_id}:checked:{run_date}",
                _with_source_bar(
                    position.to_dict(),
                    position_source_bars[position.position_id],
                    run,
                ),
            )
            for position in checked_no_action
        ]
        + pending_no_fill_events
    )
    _commit_paper_transaction(
        paths,
        events=transaction_events,
        state_updates={
            pending_path: remaining_pending,
            positions_path: updated_positions,
            _paper_accounts_path(paths, mode): _account_state_payload(paths, mode, accounts),
        },
    )
    _write_calendar_for_date(paths, run, manifest, warnings, dataset=dataset)
    _write_daily_report(
        paths,
        run,
        manifest,
        {
            "fills": len(fills),
            "closes": len(closes),
            "open_positions": len(updated_positions),
            "orders_blocked": len(blocked_orders),
            "phase": "check",
        },
        warnings,
    )
    return {
        "closes": len(closes),
        "fills": len(fills),
        "open_positions": len(updated_positions),
        "orders_blocked": len(blocked_orders),
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
    _recover_pending_transaction(paths)
    config = _config(paths)
    data_truth_root = _data_truth_root_for_mode(paths, mode)
    dataset, manifest, warnings = _load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
        universe_symbols=config.universe_symbols,
        allow_single_provider_forward=config.allow_single_provider_forward,
        data_truth_root=data_truth_root,
    )
    run = _paper_run(run_date=run_date, mode=mode, data_snapshot_id=manifest.snapshot_id)
    _ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=manifest,
        data_truth_root=data_truth_root,
    )
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
    close_source_bars: dict[str, MarketBar] = {}
    mark_source_bars: dict[str, MarketBar] = {}
    for position in [_position_from_row(row) for row in position_rows]:
        bar = _latest_bar_on_or_before(dataset, position.symbol, run_date)
        if bar is None:
            updated_positions.append(position.to_dict())
            continue
        checked, close_record = _check_position(position, bar, run, config)
        if close_record is not None:
            closes.append(close_record)
            close_source_bars[close_record.close_id] = bar
            accounts = _apply_close(accounts, close_record)
        else:
            updated_positions.append(checked.to_dict())
            marks.append(checked)
            mark_source_bars[checked.position_id] = bar
            accounts = _apply_mark(accounts, checked)
    accounts = _recalculate_unrealized_accounts(accounts, updated_positions)
    transaction_events = [
        _event(
            run,
            PaperJobPhase.CLOSE,
            close_record.strategy_id,
            close_record.symbol,
            "paper_position_closed",
            close_record.close_id,
            _with_source_bar(
                close_record.to_dict(),
                close_source_bars[close_record.close_id],
                run,
            ),
        )
        for close_record in closes
    ] + [
        _event(
            run,
            PaperJobPhase.CLOSE,
            position.strategy_id,
            position.symbol,
            "paper_position_marked_to_market",
            f"{position.position_id}:mark:{run_date}",
            _with_source_bar(
                position.to_dict(),
                mark_source_bars[position.position_id],
                run,
            ),
        )
        for position in marks
    ]
    _commit_paper_transaction(
        paths,
        events=transaction_events,
        state_updates={
            positions_path: updated_positions,
            _paper_accounts_path(paths, mode): _account_state_payload(paths, mode, accounts),
        },
    )
    _write_calendar_for_date(paths, run, manifest, warnings, dataset=dataset)
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
    paths = PaperOpsPaths.create(output_root)
    with exclusive_file_lock(paths.state / ".paper_ops_operation.lock"):
        return _run_day_unlocked(
            run_date=run_date,
            mode=mode,
            output_root=output_root,
            allow_fetch=allow_fetch,
        )


def _run_day_unlocked(
    *,
    run_date: date,
    mode: PaperRunMode,
    output_root: Path,
    allow_fetch: bool,
) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
    from intraday_scanner.v2.paper_ops.trade_blotter import verify_trade_blotter

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
    reconciliation = _reconcile_paths(PaperOpsPaths.create(output_root))
    if reconciliation.get("status") != "passed":
        raise ValueError("PaperOps daily ledger reconciliation failed")
    source_truth = verify_source_bar_truth(output_root=output_root, mode=mode)
    if source_truth.status != "passed":
        details = "; ".join(source_truth.warnings) or "no verifier reason was recorded"
        raise ValueError(f"PaperOps daily immutable source-bar verification failed: {details}")
    from intraday_scanner.v2.paper_ops.trade_blotter import _build_trade_blotter_writer

    blotter = _build_trade_blotter_writer(
        output_root=output_root,
        mode=mode.value,
        run_date=run_date.isoformat(),
    )
    blotter_verification = verify_trade_blotter(
        output_root=output_root,
        mode=mode.value,
    )
    if blotter.get("status") != "passed" or blotter_verification.get("status") != "passed":
        raise ValueError("PaperOps daily trade blotter verification failed")
    report_result = _report_paths(PaperOpsPaths.create(output_root))
    return {
        "calendar": calendar_result,
        "blotter": blotter,
        "blotter_verification": blotter_verification,
        "check": check_result,
        "close": close_result,
        "enter": enter_result,
        "reconcile": reconciliation,
        "report": report_result,
        "run_id": scan_result["run_id"],
        "scan": scan_result,
        "source_bar_truth": source_truth.to_dict(),
    }


def replay(
    *,
    start: date,
    end: date,
    output_root: Path = Path("data/v2_paper_ops"),
    allow_fetch: bool = True,
) -> dict[str, object]:
    if start > end:
        raise ValueError("PaperOps replay start date must be on or before end date")
    current = start
    trading_dates: list[date] = []
    skipped_closed_days: list[str] = []
    while current <= end:
        if not market_session(current).is_trading_day:
            skipped_closed_days.append(current.isoformat())
        else:
            trading_dates.append(current)
        current += timedelta(days=1)
    if not trading_dates:
        raise ValueError("PaperOps replay range contains no US equities trading sessions")
    # The staging root is initialized below, but promotion also depends on the
    # target's frozen policy and strategy manifests. A brand-new replay target
    # must therefore be initialized before staging is created or copied.
    init(output_root=output_root)
    paths = PaperOpsPaths.create(output_root)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{paths.root.name}_replay_staging_",
            dir=str(paths.root.parent),
        )
    )
    try:
        staging_paths = PaperOpsPaths.create(staging_root)
        for name in ("paper_ops_config.json", "execution_policy_manifest.json"):
            source_payload = read_json(paths.state / name, None)
            if source_payload is not None:
                write_json(staging_paths.state / name, source_payload)
        init(output_root=staging_root)
        staging_config = _config(staging_paths)
        _reset_mode_generated_state(
            staging_paths,
            PaperRunMode.REPLAY,
            staging_config,
        )
        for trading_date in trading_dates:
            run_day(
                run_date=trading_date,
                mode=PaperRunMode.REPLAY,
                output_root=staging_root,
                allow_fetch=allow_fetch,
            )
        verification = _verify_replay_staging(staging_root)
        _promote_replay_staging(paths, staging_paths)
    finally:
        if staging_root.exists() and staging_root.parent == paths.root.parent:
            shutil.rmtree(staging_root)
    return {
        "mode": PaperRunMode.REPLAY.value,
        "days": len(trading_dates),
        "trading_days": len(trading_dates),
        "skipped_closed_days": skipped_closed_days,
        "verification": verification,
    }


def _verify_replay_staging(staging_root: Path) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
    from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
    from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
    from intraday_scanner.v2.paper_ops.trade_blotter import (
        _build_trade_blotter_writer,
        verify_trade_blotter,
    )

    reconciliation = _reconcile_paths(PaperOpsPaths.create(staging_root))
    if reconciliation.get("status") != "passed":
        raise ValueError("PaperOps staged replay reconciliation failed")
    calendar_truth = verify_calendar_truth(output_root=staging_root)
    if calendar_truth.status != "passed":
        raise ValueError("PaperOps staged replay calendar verification failed")
    rebuilt = rebuild_ledger(output_root=staging_root)
    if rebuilt.status != "passed":
        raise ValueError("PaperOps staged replay ledger rebuild failed")
    source_truth = verify_source_bar_truth(
        output_root=staging_root,
        mode=PaperRunMode.REPLAY,
    )
    if source_truth.status != "passed":
        details = "; ".join(source_truth.warnings) or "no verifier reason was recorded"
        raise ValueError(
            f"PaperOps staged replay immutable source-bar verification failed: {details}"
        )
    blotter = _build_trade_blotter_writer(output_root=staging_root)
    blotter_verification = verify_trade_blotter(
        output_root=staging_root,
        mode=PaperRunMode.REPLAY.value,
    )
    if blotter.get("status") != "passed" or blotter_verification.get("status") != "passed":
        raise ValueError("PaperOps staged replay trade blotter verification failed")
    return {
        "blotter": blotter_verification["status"],
        "calendar_truth": calendar_truth.status,
        "ledger_rebuild": rebuilt.status,
        "reconciliation": reconciliation["status"],
        "source_bar_truth": source_truth.status,
    }


def _promote_replay_staging(
    paths: PaperOpsPaths,
    staging_paths: PaperOpsPaths,
) -> None:
    with exclusive_file_lock(paths.state / ".paper_ops_operation.lock"):
        rollback_root = Path(
            tempfile.mkdtemp(
                prefix=f".{paths.root.name}_replay_rollback_",
                dir=str(paths.root.parent),
            )
        )
        _snapshot_replay_promotion_targets(paths, rollback_root)
        try:
            ledger_path = paths.ledger / "paper_ledger.jsonl"
            staging_events = [
                event
                for event in read_jsonl(staging_paths.ledger / "paper_ledger.jsonl")
                if str(event.get("mode") or "") == PaperRunMode.REPLAY.value
            ]
            with exclusive_file_lock(jsonl_lock_path(ledger_path)):
                retained_events = [
                    event
                    for event in read_jsonl(ledger_path)
                    if str(event.get("mode") or "") != PaperRunMode.REPLAY.value
                ]
                write_jsonl(ledger_path, [*retained_events, *staging_events])
            retained_calendar = [
                row
                for row in _read_calendar_rows(paths)
                if str(row.get("mode") or "") != PaperRunMode.REPLAY.value
            ]
            staging_calendar = [
                row
                for row in _read_calendar_rows(staging_paths)
                if str(row.get("mode") or "") == PaperRunMode.REPLAY.value
            ]
            promoted_calendar = [*retained_calendar, *staging_calendar]
            write_csv(
                paths.calendar / "strategy_daily_returns.csv",
                promoted_calendar,
                CALENDAR_FIELDNAMES,
            )
            write_json(paths.calendar / "strategy_daily_returns.json", promoted_calendar)
            for state_name in (
                "replay_pending_orders.json",
                "replay_open_positions.json",
                "replay_paper_accounts.json",
            ):
                write_json(
                    paths.state / state_name,
                    read_json(staging_paths.state / state_name, {}),
                )
            _replace_replay_files(staging_paths.exports, paths.exports)
            _replace_replay_files(staging_paths.manifests, paths.manifests)
            _replace_replay_files(staging_paths.reports / "daily", paths.reports / "daily")
            staging_data_truth = staging_paths.root / "data_truth_replay"
            production_data_truth = paths.root / "data_truth_replay"
            if staging_data_truth.exists():
                if production_data_truth.exists():
                    shutil.rmtree(production_data_truth)
                shutil.copytree(staging_data_truth, production_data_truth)
            _calendar_paths(paths)
            reconciliation = _reconcile_paths(paths)
            if reconciliation.get("status") != "passed":
                raise ValueError("PaperOps promoted replay reconciliation failed")
            from intraday_scanner.v2.paper_ops.source_bar_truth import (
                verify_source_bar_truth,
            )
            from intraday_scanner.v2.paper_ops.trade_blotter import (
                _build_trade_blotter_writer,
            )

            promoted_source_truth = verify_source_bar_truth(
                output_root=paths.root,
                mode=PaperRunMode.REPLAY,
            )
            if promoted_source_truth.status != "passed":
                details = (
                    "; ".join(promoted_source_truth.warnings) or "no verifier reason was recorded"
                )
                raise ValueError(
                    f"PaperOps promoted replay immutable source-bar verification failed: {details}"
                )
            _build_trade_blotter_writer(output_root=paths.root)
            _report_paths(paths)
        except Exception:
            _restore_replay_promotion_targets(paths, rollback_root)
            raise
        finally:
            shutil.rmtree(rollback_root, ignore_errors=True)


def _snapshot_replay_promotion_targets(paths: PaperOpsPaths, backup: Path) -> None:
    for name, source in (
        ("ledger", paths.ledger),
        ("calendar", paths.calendar),
        ("exports", paths.exports),
        ("manifests", paths.manifests),
        ("reports", paths.reports),
        ("reconciliation", paths.reconciliation),
        ("data_truth_replay", paths.root / "data_truth_replay"),
    ):
        if source.exists():
            shutil.copytree(source, backup / name)
    for state_name in (
        "replay_pending_orders.json",
        "replay_open_positions.json",
        "replay_paper_accounts.json",
    ):
        source = paths.state / state_name
        if source.exists():
            target = backup / "state" / state_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _restore_replay_promotion_targets(paths: PaperOpsPaths, backup: Path) -> None:
    for name, target in (
        ("ledger", paths.ledger),
        ("calendar", paths.calendar),
        ("exports", paths.exports),
        ("manifests", paths.manifests),
        ("reports", paths.reports),
        ("reconciliation", paths.reconciliation),
        ("data_truth_replay", paths.root / "data_truth_replay"),
    ):
        if target.exists():
            shutil.rmtree(target)
        source = backup / name
        if source.exists():
            shutil.copytree(source, target)
    for state_name in (
        "replay_pending_orders.json",
        "replay_open_positions.json",
        "replay_paper_accounts.json",
    ):
        target = paths.state / state_name
        source = backup / "state" / state_name
        if source.exists():
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()


def _replace_replay_files(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for existing in target.iterdir():
        if existing.is_file() and "replay" in existing.name.lower():
            existing.unlink()
    for source_file in source.iterdir():
        if source_file.is_file() and "replay" in source_file.name.lower():
            shutil.copy2(source_file, target / source_file.name)


def calendar(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "calendar")
    paths = PaperOpsPaths.resolve(output_root)
    return _calendar_paths(paths)


def _calendar_paths(paths: PaperOpsPaths) -> dict[str, object]:
    """Render calendar artifacts for an already writer-authorized tree."""

    rows = _read_calendar_rows(paths)
    _write_calendar_matrix(paths, rows)
    _write_monthly_returns(paths, rows)
    _write_equity_and_drawdown(paths, rows)
    _write_calendar_summary(paths, rows)
    return {"calendar_rows": len(rows)}


def reconcile(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "reconcile")
    paths = PaperOpsPaths.resolve(output_root)
    return _reconcile_paths(paths)


def _reconcile_paths(paths: PaperOpsPaths) -> dict[str, object]:
    """Reconcile an already writer-authorized canonical tree."""

    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    event_id_counts = Counter(str(event.get("event_id")) for event in events)
    duplicates = sorted(event_id for event_id, count in event_id_counts.items() if count > 1)
    logical_ids: dict[str, set[str]] = {}
    for event in events:
        logical_key = _logical_event_key(event)
        if logical_key is None:
            continue
        logical_ids.setdefault(logical_key, set()).add(str(event.get("event_id") or ""))
    duplicates.extend(f"logical:{key}" for key, ids in sorted(logical_ids.items()) if len(ids) > 1)
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


def _logical_event_key(event: dict[str, object]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = str(event.get("event_type") or "")
    id_fields = {
        "paper_pick_decision": "pick_id",
        "paper_no_setup_decision": "decision_id",
        "paper_order_created": "order_id",
        "paper_fill": "fill_id",
        "paper_position_opened": "position_id",
        "paper_position_closed": "close_id",
    }
    entity_field = id_fields.get(event_type)
    entity_id = str(payload.get(entity_field) or "") if entity_field else ""
    if not entity_id and event_type in {
        "paper_order_blocked",
        "paper_order_pending_no_fill_data",
    }:
        entity_id = str(payload.get("order_id") or "")
    if not entity_id and event_type in {
        "paper_position_checked_no_action",
        "paper_position_marked_to_market",
    }:
        entity_id = str(payload.get("position_id") or "")
    if not entity_id:
        return None
    return "|".join(
        (
            str(event.get("mode") or ""),
            str(event.get("trade_date") or ""),
            event_type,
            entity_id,
        )
    )


def report(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "report")
    paths = PaperOpsPaths.resolve(output_root)
    return _report_paths(paths)


def _report_paths(paths: PaperOpsPaths) -> dict[str, object]:
    """Write report artifacts for an already writer-authorized tree."""

    rows = _read_calendar_rows(paths)
    lines = [
        "# PaperOps v1 Summary",
        "",
        f"- Calendar rows: `{len(rows)}`",
        f"- Forward days tracked: `{len({r['date'] for r in rows if r['mode'] == 'forward'})}`",
        f"- Replay days tracked: `{len({r['date'] for r in rows if r['mode'] == 'replay'})}`",
        "",
    ]
    mode_titles = {
        PaperRunMode.FORWARD.value: "Forward observed evidence",
        PaperRunMode.REPLAY.value: "Historical replay research",
        PaperRunMode.DEMO.value: "Synthetic demo only",
    }
    for mode in (
        PaperRunMode.FORWARD.value,
        PaperRunMode.REPLAY.value,
        PaperRunMode.DEMO.value,
    ):
        mode_rows = [row for row in rows if str(row.get("mode") or "") == mode]
        lines.extend([f"## {mode_titles[mode]}", ""])
        if not mode_rows:
            lines.extend(["- No rows recorded for this evidence mode.", ""])
            continue
        series_keys = sorted(
            {
                (
                    str(row.get("strategy_id") or "unknown"),
                    str(row.get("strategy_version") or "unknown"),
                    str(row.get("execution_policy_version") or "unknown"),
                )
                for row in mode_rows
            }
        )
        for strategy_id, strategy_version, execution_policy_version in series_keys:
            series_rows = [
                row
                for row in mode_rows
                if (
                    str(row.get("strategy_id") or "unknown"),
                    str(row.get("strategy_version") or "unknown"),
                    str(row.get("execution_policy_version") or "unknown"),
                )
                == (strategy_id, strategy_version, execution_policy_version)
            ]
            latest = max(series_rows, key=lambda row: str(row.get("date") or ""))
            status = str(latest.get("strategy_status") or "unknown")
            evidence_label = (
                "reference policy" if status in {"baseline", "benchmark"} else "paper strategy"
            )
            lines.append(
                f"- `{strategy_id}` version `{strategy_version}` under "
                f"`{execution_policy_version}` ({evidence_label}); latest "
                f"`{latest.get('date', 'unknown')}`, cumulative return "
                f"`{latest.get('cumulative_return_pct', 'N/A')}`, drawdown "
                f"`{latest.get('drawdown_pct', 'N/A')}`."
            )
        lines.append("")
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
    _write_calendar_for_date(paths, run, manifest, manifest.warnings, dataset=dataset)
    calendar(output_root=output_root)
    _reconcile_paths(paths)
    _report_paths(paths)
    return {"mode": "demo", "run_id": run.run_id, "snapshot_id": manifest.snapshot_id}


def _data_truth_root_for_mode(
    paths: PaperOpsPaths,
    mode: PaperRunMode,
) -> Path:
    return (
        paths.root / "data_truth_replay"
        if mode is PaperRunMode.REPLAY
        else paths.root.parent / "v2_data_truth"
    )


def _load_dataset_for_mode(
    *,
    run_date: date,
    mode: PaperRunMode,
    allow_fetch: bool = True,
    universe_symbols: tuple[str, ...] = DEFAULT_PAPEROPS_UNIVERSE,
    allow_single_provider_forward: bool = True,
    data_truth_root: Path = Path("data/v2_data_truth"),
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
        _RECONCILIATION_STATUS_BY_SNAPSHOT[manifest.snapshot_id] = "demo_only"
        return dataset, manifest, manifest.warnings
    if mode is PaperRunMode.FORWARD:
        _assert_forward_session_complete(run_date)
    normalized_universe = tuple(dict.fromkeys(symbol.upper() for symbol in universe_symbols))
    cache_key = (
        mode.value,
        run_date.isoformat(),
        allow_fetch,
        normalized_universe,
        allow_single_provider_forward,
        str(data_truth_root.resolve()),
    )
    cached = _DATASET_CACHE.get(cache_key)
    if cached is not None:
        return cached
    snapshot_as_of = run_date + timedelta(days=1)
    result = build_data_truth_snapshot(
        as_of_date=snapshot_as_of,
        allow_fetch=allow_fetch,
        symbols=normalized_universe,
        output_root=data_truth_root,
    )
    observed_symbols = set(result.dataset.symbols)
    configured_symbols = set(normalized_universe)
    missing_symbols = sorted(configured_symbols - observed_symbols)
    extra_symbols = sorted(observed_symbols - configured_symbols)
    if missing_symbols or extra_symbols:
        raise ValueError(
            "PaperOps DataTruth snapshot symbol set does not exactly match the configured "
            f"universe; missing={missing_symbols}, extra={extra_symbols}"
        )
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
        raise ValueError(f"forward PaperOps blocks DataTruth status {result.reconciliation.status}")
    if (
        mode is PaperRunMode.FORWARD
        and result.reconciliation.status == "single_provider_unreconciled"
        and not allow_single_provider_forward
    ):
        raise ValueError(
            "forward PaperOps blocks single-provider evidence because "
            "allow_single_provider_forward is false"
        )
    if result.manifest.validation_status not in {"passed", "passed_with_warnings"}:
        raise ValueError(
            "PaperOps blocks DataTruth manifest validation status "
            f"{result.manifest.validation_status}"
        )
    if mode in {PaperRunMode.FORWARD, PaperRunMode.REPLAY} and (
        result.manifest.accepted_end != run_date.isoformat()
    ):
        raise ValueError(
            "PaperOps requires a completed configured-universe bar for the run date; "
            f"latest accepted date is {result.manifest.accepted_end}"
        )
    stale_symbols = sorted(
        symbol
        for symbol in normalized_universe
        if not result.dataset.bars_by_symbol.get(symbol)
        or result.dataset.bars_by_symbol[symbol][-1].timestamp.date() != run_date
    )
    if mode in {PaperRunMode.FORWARD, PaperRunMode.REPLAY} and stale_symbols:
        raise ValueError(
            "PaperOps requires an exact completed run-date bar for every configured "
            "symbol; stale or missing: " + ", ".join(stale_symbols)
        )
    warnings = tuple(result.manifest.warnings) + tuple(result.reconciliation.warnings)
    _RECONCILIATION_STATUS_BY_SNAPSHOT[result.manifest.snapshot_id] = str(
        result.reconciliation.status
    )
    loaded = (result.dataset, result.manifest, tuple(dict.fromkeys(warnings)))
    _DATASET_CACHE[cache_key] = loaded
    return loaded


def _assert_forward_session_complete(run_date: date) -> None:
    current = _current_utc_time()
    if current.tzinfo is None:
        raise ValueError("PaperOps current time must include a timezone")
    market_now = current.astimezone(MARKET_TIMEZONE)
    if run_date != market_now.date():
        raise ValueError(
            "forward PaperOps requires the current market date; use replay for "
            "historical/catch-up evidence"
        )
    session = market_session(run_date)
    if not session.is_trading_day or session.close_time_et is None:
        raise ValueError("forward PaperOps requires an open US equities trading session")
    scheduled_close = time.fromisoformat(session.close_time_et)
    if market_now.time() < scheduled_close:
        raise ValueError(
            "forward PaperOps blocks a same-day run before the US equities regular "
            f"session is complete (scheduled close {session.close_time_et} "
            f"America/New_York for {run_date.isoformat()})"
        )


def _current_utc_time() -> datetime:
    return datetime.now(timezone.utc)


def _paper_run(*, run_date: date, mode: PaperRunMode, data_snapshot_id: str) -> PaperRun:
    return PaperRun(
        run_id=stable_id("paper_ops", mode.value, run_date.isoformat(), data_snapshot_id),
        mode=mode,
        run_date=run_date.isoformat(),
        data_snapshot_id=data_snapshot_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _strategy_configs(
    config: PaperOpsConfig,
    strategies: tuple[StrategySpec, ...] | None = None,
) -> tuple[PaperStrategyConfig, ...]:
    configs: list[PaperStrategyConfig] = []
    for strategy in strategies or build_strategy_catalog():
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
                execution_policy_version=config.execution_policy_version,
            )
        )
    return tuple(configs)


def _eligible(
    strategy: StrategySpec,
    config: PaperOpsConfig,
    paths: PaperOpsPaths | None = None,
) -> bool:
    if strategy.status in {"quarantined", "rejected", "parked", "baseline", "benchmark"}:
        return False
    if paths is not None and _governance_block_reason(paths, strategy, config) is not None:
        return False
    if strategy.status == "experimental":
        return config.allow_experimental
    return True


def _strategies_eligible_for_run(
    paths: PaperOpsPaths,
    *,
    config: PaperOpsConfig,
    run_date: date,
    mode: PaperRunMode,
) -> tuple[StrategySpec, ...]:
    """Return catalog strategies permitted to produce decisions for this run."""

    return tuple(
        strategy
        for strategy in build_strategy_catalog()
        if _eligible(strategy, config, paths)
        and _series_is_eligible_for_run(
            paths,
            run_date=run_date,
            mode=mode,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            execution_policy_version=config.execution_policy_version,
            strategy_semantics_fingerprint=_strategy_semantics_fingerprint(strategy),
        )
    )


def _backtest_results(
    dataset: MarketDataset,
    strategies: tuple[StrategySpec, ...],
    config: PaperOpsConfig,
) -> dict[str, BacktestResult]:
    # Local import avoids a module cycle: the adapter intentionally reuses the
    # canonical PaperOps lifecycle functions defined in this module.
    from intraday_scanner.v2.paper_ops.lifecycle_backtest import (
        PaperOpsLifecycleBacktestEngine,
    )

    return PaperOpsLifecycleBacktestEngine(config).run(strategies, dataset)


def _picks_from_scan(
    scan_output: ScanOutput,
    strategies: tuple[StrategySpec, ...],
    run: PaperRun,
    config: PaperOpsConfig,
    inherited_warnings: tuple[str, ...],
) -> tuple[PaperPick, ...]:
    status_by_id = {strategy.strategy_id: strategy.status for strategy in strategies}
    semantics_by_id = {
        strategy.strategy_id: _strategy_semantics_fingerprint(strategy) for strategy in strategies
    }
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
            config.execution_policy_version,
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
                execution_policy_version=config.execution_policy_version,
                strategy_semantics_fingerprint=semantics_by_id.get(
                    card.strategy_id,
                    "unknown",
                ),
            )
        )
    return tuple(picks)


def _no_setup_decision(
    card: ScanCard,
    run: PaperRun,
    config: PaperOpsConfig,
    inherited_warnings: tuple[str, ...],
    strategy_semantics_fingerprint: str,
) -> dict[str, object]:
    decision_id = stable_id(
        run.mode.value,
        run.run_date,
        card.strategy_id,
        card.strategy_version,
        config.execution_policy_version,
        card.symbol,
        card.timestamp.isoformat(),
        "no_setup",
    )
    return {
        "account_return_effect_pct": 0.0,
        "decision_id": decision_id,
        "decision_status": "no_setup",
        "direction": "flat",
        "evidence": list(card.evidence),
        "market_date": run.run_date,
        "mode": run.mode.value,
        "reason": card.entry_trigger,
        "research_only": True,
        "run_id": run.run_id,
        "schema_version": "v2.paper_strategy_decision.v1",
        "signal_time": card.timestamp.isoformat(),
        "strategy_id": card.strategy_id,
        "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
        "strategy_version": card.strategy_version,
        "execution_policy_version": config.execution_policy_version,
        "symbol": card.symbol,
        "trade_return_eligible": False,
        "trade_return_pct": None,
        "warnings": list(dict.fromkeys(inherited_warnings + card.warnings)),
    }


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
    *,
    equity_basis: float | None = None,
) -> PaperOrder:
    assert pick.stop is not None
    assert pick.risk_per_unit is not None
    if pick.execution_policy_version != config.execution_policy_version:
        raise ValueError("PaperOps pick execution policy does not match the active engine policy")
    active_equity = max(equity_basis if equity_basis is not None else config.starting_equity, 0.0)
    modeled_risk_per_unit = _modeled_order_stop_loss_per_unit(pick, config)
    quantity = max(
        int((active_equity * config.risk_per_trade_pct) / modeled_risk_per_unit)
        if modeled_risk_per_unit > 0
        else 0,
        0,
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
        risk_per_unit=modeled_risk_per_unit,
        reward_per_unit=pick.reward_per_unit,
        reward_risk=pick.reward_risk,
        risk_budget=active_equity * config.risk_per_trade_pct,
        quantity=quantity,
        notional_exposure=quantity * pick.entry_reference,
        max_loss_estimate=quantity * modeled_risk_per_unit,
        strategy_equity_basis=active_equity,
        execution_policy_version=pick.execution_policy_version,
        strategy_semantics_fingerprint=pick.strategy_semantics_fingerprint,
        warnings=pick.warnings,
    )


def _modeled_order_stop_loss_per_unit(
    pick: PaperPick,
    config: PaperOpsConfig,
) -> float:
    assert pick.stop is not None
    rate = config.slippage_bps / 10_000.0
    entry_fill = (
        pick.entry_reference * (1 + rate)
        if pick.direction == Direction.LONG
        else pick.entry_reference * (1 - rate)
    )
    stop_fill = (
        pick.stop * (1 - rate) if pick.direction == Direction.LONG else pick.stop * (1 + rate)
    )
    gross_loss = max(0.0, -_pnl(pick.direction, entry_fill, stop_fill, 1))
    entry_fee = entry_fill * config.fee_bps / 10_000.0
    exit_fee = stop_fill * config.fee_bps / 10_000.0
    return gross_loss + entry_fee + exit_fee


def _fill_entry_block_reason(
    order: PaperOrder,
    *,
    fill: PaperFill,
    position: PaperPosition,
    fill_bar: MarketBar,
    position_rows: list[dict[str, object]],
    pending_rows: list[dict[str, object]],
    account: StrategyPaperAccount,
    config: PaperOpsConfig,
    daily_closed_net: float,
) -> str | None:
    if (
        order.direction == Direction.LONG
        and fill_bar.open <= order.stop
        or order.direction == Direction.SHORT
        and fill_bar.open >= order.stop
    ):
        return "gap_through_stop"
    if order.target is not None and (
        order.direction == Direction.LONG
        and fill.fill_price >= order.target
        or order.direction == Direction.SHORT
        and fill.fill_price <= order.target
    ):
        return "gap_through_target"
    actual_risk = _position_risk(position.to_dict(), config)
    if actual_risk <= 0:
        return "invalid_fill_risk"
    if order.target is not None:
        rate = config.slippage_bps / 10_000.0
        target_fill = (
            order.target * (1 - rate)
            if order.direction == Direction.LONG
            else order.target * (1 + rate)
        )
        gross_reward = _pnl(
            order.direction,
            fill.fill_price,
            target_fill,
            fill.quantity,
        )
        target_fee = target_fill * fill.quantity * config.fee_bps / 10_000.0
        net_reward = gross_reward - fill.fee - target_fee
        if net_reward <= 0 or net_reward / actual_risk < config.min_reward_risk:
            return "fill_reward_risk_below_threshold"
    return _order_entry_block_reason(
        order,
        position_rows=position_rows,
        pending_rows=pending_rows,
        account=account,
        config=config,
        daily_closed_net=daily_closed_net,
        candidate_max_loss=actual_risk,
        candidate_notional=fill.fill_price * fill.quantity,
    )


def _strategy_version_key(
    row: PaperOrder | dict[str, object],
) -> tuple[str, str, str, str]:
    if isinstance(row, PaperOrder):
        return (
            row.strategy_id,
            row.strategy_version,
            row.execution_policy_version,
            row.strategy_semantics_fingerprint,
        )
    return (
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("execution_policy_version") or "legacy_unspecified"),
        str(row.get("strategy_semantics_fingerprint") or "unknown"),
    )


def _order_entry_block_reason(
    order: PaperOrder,
    *,
    position_rows: list[dict[str, object]],
    pending_rows: list[dict[str, object]],
    account: StrategyPaperAccount,
    config: PaperOpsConfig,
    daily_closed_net: float,
    candidate_max_loss: float | None = None,
    candidate_notional: float | None = None,
) -> str | None:
    key = _strategy_version_key(order)
    matching_positions = [row for row in position_rows if _strategy_version_key(row) == key]
    matching_pending = [row for row in pending_rows if _strategy_version_key(row) == key]
    if order.quantity <= 0:
        return "zero_quantity"
    if any(
        str(row.get("symbol") or "") == order.symbol
        for row in matching_positions + matching_pending
    ):
        return "duplicate_strategy_symbol_exposure"
    reservation_count = len(matching_positions) + len(matching_pending)
    if reservation_count >= config.max_concurrent_positions:
        return "max_concurrent_positions"
    open_risk = sum(_position_risk(row, config) for row in matching_positions) + sum(
        float(row.get("max_loss_estimate") or 0.0) for row in matching_pending
    )
    order_risk = order.max_loss_estimate if candidate_max_loss is None else candidate_max_loss
    risk_limit = max(account.current_equity, 0.0) * config.max_open_risk_pct
    if order_risk > order.risk_budget + 1e-9:
        return "fill_risk_budget_exceeded"
    if open_risk + order_risk > risk_limit + 1e-9:
        return "max_open_risk"
    gross_exposure = sum(_position_gross_exposure(row) for row in matching_positions) + sum(
        abs(float(row.get("notional_exposure") or 0.0)) for row in matching_pending
    )
    order_notional = (
        abs(candidate_notional) if candidate_notional is not None else abs(order.notional_exposure)
    )
    gross_limit = max(account.current_equity, 0.0) * config.max_gross_exposure_pct
    if gross_exposure + order_notional > gross_limit + 1e-9:
        return "max_gross_exposure"
    daily_loss_limit = max(account.current_equity, 0.0) * config.max_daily_loss_pct
    if daily_closed_net <= -daily_loss_limit and daily_closed_net < 0:
        return "max_daily_loss"
    return None


def _position_gross_exposure(row: dict[str, object]) -> float:
    try:
        mark = float(row.get("last_mark_price") or row.get("entry_price") or 0.0)
        quantity = int(row.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0.0
    return abs(mark * quantity)


def _position_risk(row: dict[str, object], config: PaperOpsConfig) -> float:
    try:
        direction = str(row.get("direction") or "")
        entry_price = float(row.get("entry_price") or 0.0)
        stop = float(row.get("stop") or 0.0)
        quantity = int(row.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0.0
    if direction not in {Direction.LONG, Direction.SHORT} or quantity <= 0:
        return 0.0
    rate = config.slippage_bps / 10_000.0
    stop_fill = stop * (1 - rate) if direction == Direction.LONG else stop * (1 + rate)
    gross_loss = max(0.0, -_pnl(direction, entry_price, stop_fill, quantity))
    raw_entry_fee = row.get("entry_fee")
    entry_fee = (
        float(raw_entry_fee)
        if raw_entry_fee not in {None, ""}
        else entry_price * quantity * config.fee_bps / 10_000.0
    )
    exit_fee = stop_fill * quantity * config.fee_bps / 10_000.0
    return gross_loss + entry_fee + exit_fee


def _daily_closed_net_by_strategy(
    events: list[dict[str, object]],
    run_date: str,
    mode: PaperRunMode,
) -> dict[tuple[str, str, str, str], float]:
    totals: dict[tuple[str, str, str, str], float] = {}
    for event in events:
        payload = event.get("payload")
        if (
            event.get("trade_date") != run_date
            or event.get("event_type") != "paper_position_closed"
            or not _event_matches_mode(event, mode)
            or not isinstance(payload, dict)
        ):
            continue
        key = _strategy_version_key(payload)
        totals[key] = totals.get(key, 0.0) + float(payload.get("net_pnl") or 0.0)
    return totals


def _blocked_order_payload(
    order: PaperOrder,
    reason: str,
    run: PaperRun,
    *,
    source_bar: MarketBar | None = None,
) -> dict[str, object]:
    payload = {
        **order.to_dict(),
        "blocked_at": run.created_at,
        "decision": "blocked",
        "lifecycle_run_id": run.run_id,
        "origin_run_id": order.run_id,
        "reason": reason,
        "research_only": True,
    }
    return _with_source_bar(payload, source_bar, run) if source_bar is not None else payload


def _pending_order_lifecycle_payload(
    order: PaperOrder,
    run: PaperRun,
) -> dict[str, object]:
    return {
        **order.to_dict(),
        "lifecycle_run_id": run.run_id,
        "origin_run_id": order.run_id,
    }


def _with_source_bar(
    payload: dict[str, object],
    bar: MarketBar,
    run: PaperRun,
) -> dict[str, object]:
    source_bar: dict[str, object] = {
        "close": bar.close,
        "high": bar.high,
        "low": bar.low,
        "open": bar.open,
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "volume": bar.volume,
    }
    source_bar_sha256 = hashlib.sha256(
        json.dumps(source_bar, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "data_snapshot_id": run.data_snapshot_id,
        "source_bar": source_bar,
        "source_bar_sha256": source_bar_sha256,
    }


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
        execution_policy_version=str(row.get("execution_policy_version") or "legacy_unspecified"),
        strategy_semantics_fingerprint=str(row.get("strategy_semantics_fingerprint") or "unknown"),
    )


def _validate_picks_for_run(
    picks: tuple[PaperPick, ...],
    run: PaperRun,
    config: PaperOpsConfig,
    paths: PaperOpsPaths,
) -> None:
    registry = {
        (str(row.get("strategy_id") or ""), str(row.get("strategy_version") or "")): str(
            row.get("strategy_semantics_fingerprint") or "unknown"
        )
        for row in _strategy_registry(paths)
    }
    mismatches = [
        pick.pick_id
        for pick in picks
        if pick.run_id != run.run_id
        or pick.mode is not run.mode
        or pick.trade_date != run.run_date
        or pick.execution_policy_version != config.execution_policy_version
        or pick.strategy_semantics_fingerprint
        != registry.get((pick.strategy_id, pick.strategy_version), "unknown")
    ]
    if mismatches:
        raise ValueError(
            "PaperOps picks do not match the active run snapshot/date/mode/policy: "
            + ", ".join(sorted(mismatches))
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
        strategy_version=order.strategy_version,
        symbol=order.symbol,
        fill_time=bar.timestamp.isoformat(),
        fill_price=fill_price,
        quantity=order.quantity,
        fee=notional * config.fee_bps / 10_000.0,
        slippage=abs(fill_price - bar.open) * order.quantity,
        execution_policy_version=order.execution_policy_version,
        strategy_semantics_fingerprint=order.strategy_semantics_fingerprint,
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
        execution_policy_version=order.execution_policy_version,
        strategy_semantics_fingerprint=order.strategy_semantics_fingerprint,
        entry_fee=fill.fee,
        unrealized_pnl=-fill.fee,
    )


def _check_position(
    position: PaperPosition,
    bar: MarketBar,
    run: PaperRun,
    config: PaperOpsConfig,
) -> tuple[PaperPosition, PaperClose | None]:
    opened_at = datetime.fromisoformat(position.opened_at)
    if bar.timestamp < opened_at:
        raise ValueError(
            "PaperOps cannot evaluate a position with a market bar before its open time"
        )
    stop_gap = (
        bar.open <= position.stop
        if position.direction == Direction.LONG
        else bar.open >= position.stop
    )
    if stop_gap:
        return position, _close_position(
            position,
            bar.open,
            PaperCloseReason.STOP,
            bar,
            run,
            config,
        )
    if position.target is not None:
        target_gap = (
            bar.open >= position.target
            if position.direction == Direction.LONG
            else bar.open <= position.target
        )
        if target_gap:
            return position, _close_position(
                position,
                bar.open,
                PaperCloseReason.TARGET,
                bar,
                run,
                config,
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
    if (bar.timestamp.date() - opened_at.date()).days >= PAPER_TIMEOUT_DAYS:
        return position, _close_position(
            position, bar.close, PaperCloseReason.TIMEOUT, bar, run, config
        )
    unrealized = (
        _pnl(position.direction, position.entry_price, bar.close, position.quantity)
        - position.entry_fee
    )
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
        execution_policy_version=position.execution_policy_version,
        strategy_semantics_fingerprint=position.strategy_semantics_fingerprint,
        entry_fee=position.entry_fee,
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
    risk_amount = _position_risk(position.to_dict(), config)
    net = gross - position.entry_fee - fee
    return PaperClose(
        close_id=stable_id("close", position.position_id, bar.timestamp.isoformat(), reason.value),
        position_id=position.position_id,
        run_id=run.run_id,
        mode=run.mode,
        strategy_id=position.strategy_id,
        strategy_version=position.strategy_version,
        symbol=position.symbol,
        close_time=bar.timestamp.isoformat(),
        close_price=close_price,
        close_reason=reason,
        gross_pnl=gross,
        net_pnl=net,
        r_multiple=net / risk_amount if risk_amount else 0.0,
        fee=fee,
        slippage=slippage,
        entry_fee=position.entry_fee,
        execution_policy_version=position.execution_policy_version,
        strategy_semantics_fingerprint=position.strategy_semantics_fingerprint,
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
        execution_policy_version=account.execution_policy_version,
        strategy_semantics_fingerprint=account.strategy_semantics_fingerprint,
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
        execution_policy_version=account.execution_policy_version,
        strategy_semantics_fingerprint=account.strategy_semantics_fingerprint,
    )
    return accounts


def _recalculate_unrealized_accounts(
    accounts: dict[str, StrategyPaperAccount],
    position_rows: list[dict[str, object]],
) -> dict[str, StrategyPaperAccount]:
    unrealized_by_strategy: dict[tuple[str, str, str, str], float] = {}
    for row in position_rows:
        strategy_id = str(row.get("strategy_id", ""))
        if not strategy_id:
            continue
        key = _strategy_version_key(row)
        unrealized_by_strategy[key] = unrealized_by_strategy.get(key, 0.0) + float(
            row.get("unrealized_pnl", 0.0)
        )
    recalculated: dict[str, StrategyPaperAccount] = {}
    for strategy_id, account in accounts.items():
        unrealized = unrealized_by_strategy.get(
            (
                strategy_id,
                account.strategy_version,
                account.execution_policy_version,
                account.strategy_semantics_fingerprint,
            ),
            0.0,
        )
        recalculated[strategy_id] = StrategyPaperAccount(
            strategy_id=account.strategy_id,
            strategy_version=account.strategy_version,
            starting_equity=account.starting_equity,
            current_equity=account.starting_equity + account.realized_pnl + unrealized,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=unrealized,
            execution_policy_version=account.execution_policy_version,
            strategy_semantics_fingerprint=account.strategy_semantics_fingerprint,
        )
    return recalculated


def _write_calendar_for_date(
    paths: PaperOpsPaths,
    run: PaperRun,
    manifest: DataTruthManifest,
    warnings: tuple[str, ...],
    dataset: MarketDataset | None = None,
) -> None:
    config = _config(paths)
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    existing_calendar_rows = _read_calendar_rows(paths)
    accounts = _accounts(paths, run.mode)
    strategy_rows = _strategy_registry(paths)
    position_rows = _state_rows(_open_positions_path(paths, run.mode), run.mode)
    pending_rows = _state_rows(_pending_orders_path(paths, run.mode), run.mode)
    rows: list[dict[str, object]] = []
    for strategy in strategy_rows:
        strategy_id = str(strategy["strategy_id"])
        strategy_version = str(strategy["strategy_version"])
        strategy_semantics_fingerprint = str(
            strategy.get("strategy_semantics_fingerprint") or "unknown"
        )
        execution_policy_version = str(
            strategy.get("execution_policy_version") or config.execution_policy_version
        )
        if not _series_is_eligible_for_run(
            paths,
            run_date=date.fromisoformat(run.run_date),
            mode=run.mode,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            execution_policy_version=execution_policy_version,
            strategy_semantics_fingerprint=strategy_semantics_fingerprint,
        ):
            continue
        account = accounts[strategy_id]
        day_events = [
            event
            for event in events
            if event.get("trade_date") == run.run_date
            and event.get("strategy_id") == strategy_id
            and _event_matches_mode(event, run.mode)
            and _event_matches_strategy_version(
                event,
                strategy_version,
                account.execution_policy_version,
                str(strategy.get("strategy_semantics_fingerprint") or "unknown"),
            )
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
        open_count = sum(
            1
            for row in position_rows
            if _strategy_version_key(row)
            == (
                strategy_id,
                strategy_version,
                account.execution_policy_version,
                strategy_semantics_fingerprint,
            )
        )
        pending_count = sum(
            1
            for row in pending_rows
            if _strategy_version_key(row)
            == (
                strategy_id,
                strategy_version,
                account.execution_policy_version,
                strategy_semantics_fingerprint,
            )
        )
        unrealized = account.unrealized_pnl
        ending = account.current_equity
        prior_rows = sorted(
            (
                row
                for row in existing_calendar_rows
                if str(row.get("mode") or "") == run.mode.value
                and str(row.get("strategy_id") or "") == strategy_id
                and str(row.get("strategy_version") or "") == str(strategy["strategy_version"])
                and str(row.get("execution_policy_version") or "")
                == account.execution_policy_version
                and str(row.get("strategy_semantics_fingerprint") or "unknown")
                == strategy_semantics_fingerprint
                and str(row.get("date") or "") < run.run_date
            ),
            key=lambda row: str(row.get("date") or ""),
        )
        previous_ending = (
            float(prior_rows[-1]["ending_equity"])
            if prior_rows and prior_rows[-1].get("ending_equity") not in {None, ""}
            else account.starting_equity
        )
        daily_pnl = ending - previous_ending
        historical_peak = max(
            [account.starting_equity]
            + [
                float(row["ending_equity"])
                for row in prior_rows
                if row.get("ending_equity") not in {None, ""}
            ]
        )
        peak_with_today = max(historical_peak, ending)
        row = StrategyCalendarRow(
            date=run.run_date,
            mode=run.mode,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            strategy_status=str(strategy["strategy_status"]),
            data_snapshot_id=manifest.snapshot_id,
            starting_equity=account.starting_equity,
            ending_equity=ending,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=daily_pnl,
            daily_return_pct=(daily_pnl / previous_ending if previous_ending else 0.0),
            cumulative_return_pct=(ending - account.starting_equity) / account.starting_equity,
            drawdown_pct=(ending - peak_with_today) / peak_with_today,
            trades_opened=len(fills),
            trades_closed=len(closes),
            pending_orders=pending_count,
            open_positions=open_count,
            wins=sum(1 for event in closes if float(event["payload"].get("net_pnl", 0.0)) > 0),
            losses=sum(1 for event in closes if float(event["payload"].get("net_pnl", 0.0)) < 0),
            flats=sum(1 for event in closes if float(event["payload"].get("net_pnl", 0.0)) == 0),
            average_r=sum(r_values) / len(r_values) if r_values else 0.0,
            expectancy_r=sum(r_values) / len(r_values) if r_values else 0.0,
            exposure_pct=(
                sum(
                    _position_gross_exposure(position)
                    for position in position_rows
                    if _strategy_version_key(position)
                    == (
                        strategy_id,
                        strategy_version,
                        account.execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                )
                / ending
                if ending > 0
                else 0.0
            ),
            fees_paid=fees,
            slippage_estimate=slippage,
            warnings=warnings,
            run_id=run.run_id,
            execution_policy_version=account.execution_policy_version,
            strategy_semantics_fingerprint=strategy_semantics_fingerprint,
        )
        rows.append(row.to_dict())
    if dataset is not None:
        rows.extend(
            _reference_calendar_rows(
                existing_calendar_rows,
                run=run,
                manifest=manifest,
                dataset=dataset,
                starting_equity=config.starting_equity,
            )
        )
    upsert_rows(
        paths.calendar / "strategy_daily_returns.csv",
        rows,
        (
            "date",
            "mode",
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
        ),
        CALENDAR_FIELDNAMES,
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


def _reference_calendar_rows(
    existing_rows: list[dict[str, object]],
    *,
    run: PaperRun,
    manifest: DataTruthManifest,
    dataset: MarketDataset,
    starting_equity: float = 100_000.0,
) -> list[dict[str, object]]:
    cash_row = _reference_calendar_row(
        existing_rows,
        run=run,
        manifest=manifest,
        strategy_id="cash_no_trade_baseline",
        strategy_version="v1.0",
        execution_policy_version="cash_zero_interest_v1",
        strategy_status="baseline",
        daily_return=0.0,
        warning="cash reference assumes zero interest; it is not a trade return",
        starting_equity=starting_equity,
    )
    symbol_returns: list[float] = []
    for symbol in dataset.symbols:
        eligible = [
            bar
            for bar in dataset.bars_by_symbol.get(symbol, ())
            if bar.timestamp.date() <= date.fromisoformat(run.run_date)
        ]
        if len(eligible) < 2 or eligible[-1].timestamp.date().isoformat() != run.run_date:
            return [cash_row]
        prior_close = eligible[-2].close
        if prior_close <= 0:
            return [cash_row]
        symbol_returns.append((eligible[-1].close - prior_close) / prior_close)
    if not symbol_returns:
        return [cash_row]
    benchmark_row = _reference_calendar_row(
        existing_rows,
        run=run,
        manifest=manifest,
        strategy_id="benchmark_buy_hold_equal_weight",
        strategy_version="v1.0",
        execution_policy_version="equal_weight_close_to_close_v1",
        strategy_status="benchmark",
        daily_return=sum(symbol_returns) / len(symbol_returns),
        warning=(
            "equal-weight configured-universe close-to-close benchmark; research comparison only"
        ),
        starting_equity=starting_equity,
    )
    return [cash_row, benchmark_row]


def _reference_calendar_row(
    existing_rows: list[dict[str, object]],
    *,
    run: PaperRun,
    manifest: DataTruthManifest,
    strategy_id: str,
    strategy_version: str,
    execution_policy_version: str,
    strategy_status: str,
    daily_return: float,
    warning: str,
    starting_equity: float,
) -> dict[str, object]:
    prior_rows = sorted(
        (
            row
            for row in existing_rows
            if str(row.get("mode") or "") == run.mode.value
            and str(row.get("strategy_id") or "") == strategy_id
            and str(row.get("strategy_version") or "") == strategy_version
            and str(row.get("execution_policy_version") or "") == execution_policy_version
            and str(row.get("date") or "") < run.run_date
        ),
        key=lambda row: str(row.get("date") or ""),
    )
    previous_ending = (
        float(prior_rows[-1]["ending_equity"])
        if prior_rows and prior_rows[-1].get("ending_equity") not in {None, ""}
        else starting_equity
    )
    ending_equity = previous_ending * (1.0 + daily_return)
    historical_peak = max(
        [starting_equity]
        + [
            float(row["ending_equity"])
            for row in prior_rows
            if row.get("ending_equity") not in {None, ""}
        ]
    )
    peak = max(historical_peak, ending_equity)
    return StrategyCalendarRow(
        date=run.run_date,
        mode=run.mode,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_status=strategy_status,
        data_snapshot_id=manifest.snapshot_id,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        realized_pnl=0.0,
        unrealized_pnl=ending_equity - starting_equity,
        total_pnl=ending_equity - previous_ending,
        daily_return_pct=daily_return,
        cumulative_return_pct=(ending_equity - starting_equity) / starting_equity,
        drawdown_pct=(ending_equity - peak) / peak if peak else 0.0,
        trades_opened=0,
        trades_closed=0,
        pending_orders=0,
        open_positions=0,
        wins=0,
        losses=0,
        flats=0,
        average_r=0.0,
        expectancy_r=0.0,
        exposure_pct=0.0,
        fees_paid=0.0,
        slippage_estimate=0.0,
        warnings=(warning,),
        run_id=run.run_id,
        execution_policy_version=execution_policy_version,
    ).to_dict()


def _event_matches_mode(event: dict[str, object], mode: PaperRunMode) -> bool:
    if str(event.get("mode") or "") != mode.value:
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return True
    payload_mode = payload.get("mode")
    if isinstance(payload_mode, PaperRunMode):
        payload_mode = payload_mode.value
    return payload_mode in {None, "", mode.value}


def _event_matches_strategy_version(
    event: dict[str, object],
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> bool:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    payload_strategy_version = str(payload.get("strategy_version") or "unknown")
    payload_policy = str(payload.get("execution_policy_version") or "legacy_unspecified")
    payload_fingerprint = str(payload.get("strategy_semantics_fingerprint") or "unknown")
    return (
        payload_strategy_version == strategy_version
        and payload_policy == execution_policy_version
        and payload_fingerprint == strategy_semantics_fingerprint
    )


def _calendar_series_key(row: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(row.get("strategy_id") or "unknown"),
        str(row.get("strategy_version") or "unknown"),
        str(row.get("execution_policy_version") or "legacy_unspecified"),
    )


def _write_calendar_matrix(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    dates = sorted({str(row["date"]) for row in rows})
    modes = sorted({str(row["mode"]) for row in rows})
    series = sorted({_calendar_series_key(row) for row in rows})
    id_counts = {
        strategy_id: len({item for item in series if item[0] == strategy_id})
        for strategy_id, _, _ in series
    }
    columns = {
        item: (item[0] if id_counts[item[0]] == 1 else f"{item[0]}@{item[1]}@{item[2]}")
        for item in series
    }
    matrix_rows: list[dict[str, object]] = []
    for row_date in dates:
        for mode in modes:
            mode_rows = [item for item in rows if item["date"] == row_date and item["mode"] == mode]
            if not mode_rows:
                continue
            row: dict[str, object] = {"date": row_date, "mode": mode}
            aggregate = 0.0
            count = 0
            for item in series:
                match = next(
                    (row for row in mode_rows if _calendar_series_key(row) == item),
                    None,
                )
                column = columns[item]
                if match is None:
                    row[column] = ""
                    continue
                value = float(match["daily_return_pct"])
                row[column] = value
                aggregate += value
                count += 1
            row["aggregate"] = aggregate / count if count else 0.0
            matrix_rows.append(row)
    write_csv(
        paths.calendar / "strategy_calendar_matrix.csv",
        matrix_rows,
        ("date", "mode", *tuple(columns[item] for item in series), "aggregate"),
    )


def _write_monthly_returns(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    output: list[dict[str, object]] = []
    keys = {(str(row["date"])[:7], str(row["mode"]), *_calendar_series_key(row)) for row in rows}
    for key in sorted(keys):
        month, mode, strategy_id, strategy_version, execution_policy_version = key
        matches = [
            row
            for row in rows
            if str(row["date"]).startswith(month)
            and row["mode"] == mode
            and _calendar_series_key(row)
            == (strategy_id, strategy_version, execution_policy_version)
        ]
        matches.sort(key=lambda row: str(row["date"]))
        monthly_growth = 1.0
        for row in matches:
            monthly_growth *= 1.0 + float(row["daily_return_pct"])
        monthly_return = monthly_growth - 1.0
        output.append(
            {
                "month": month,
                "mode": mode,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
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
            "mode",
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "monthly_return_pct",
            "cumulative_return_pct",
            "win_days",
            "loss_days",
            "flat_days",
            "max_drawdown_pct",
        ),
    )


def _write_equity_and_drawdown(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    equity_rows: list[dict[str, object]] = []
    drawdown_rows: list[dict[str, object]] = []
    peaks: dict[tuple[str, str, str, str], float] = {}
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            str(row["mode"]),
            *_calendar_series_key(row),
            str(row["date"]),
        ),
    )
    for row in ordered_rows:
        mode = str(row["mode"])
        strategy_id, strategy_version, execution_policy_version = _calendar_series_key(row)
        starting_equity = float(row["starting_equity"])
        ending_equity = float(row["ending_equity"])
        key = (mode, strategy_id, strategy_version, execution_policy_version)
        peak_equity = max(peaks.get(key, starting_equity), starting_equity, ending_equity)
        peaks[key] = peak_equity
        equity_rows.append(
            {
                "date": row["date"],
                "mode": mode,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "equity": ending_equity,
                "cumulative_return_pct": row["cumulative_return_pct"],
            }
        )
        drawdown_rows.append(
            {
                "date": row["date"],
                "mode": mode,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "equity": ending_equity,
                "peak_equity": peak_equity,
                "drawdown_pct": (
                    (ending_equity - peak_equity) / peak_equity if peak_equity else 0.0
                ),
            }
        )
    write_csv(
        paths.calendar / "strategy_equity_curves.csv",
        equity_rows,
        (
            "date",
            "mode",
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "equity",
            "cumulative_return_pct",
        ),
    )
    write_csv(
        paths.calendar / "strategy_drawdowns.csv",
        drawdown_rows,
        (
            "date",
            "mode",
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "equity",
            "peak_equity",
            "drawdown_pct",
        ),
    )


def _write_calendar_summary(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    if not rows:
        summary = "# PaperOps Calendar Summary\n\nNo calendar rows yet.\n"
    else:
        lines = [
            "# PaperOps Calendar Summary",
            "",
            "Evidence modes, strategy versions, and execution policies are reported separately.",
            "",
        ]
        mode_titles = {
            PaperRunMode.FORWARD.value: "Forward observed evidence",
            PaperRunMode.REPLAY.value: "Historical replay research",
            PaperRunMode.DEMO.value: "Synthetic demo only",
        }
        for mode in (
            PaperRunMode.FORWARD.value,
            PaperRunMode.REPLAY.value,
            PaperRunMode.DEMO.value,
        ):
            mode_rows = [row for row in rows if str(row.get("mode") or "") == mode]
            lines.extend([f"## {mode_titles[mode]}", ""])
            if not mode_rows:
                lines.extend(["- No rows recorded for this evidence mode.", ""])
                continue
            latest_date = max(str(row.get("date") or "") for row in mode_rows)
            latest = [row for row in mode_rows if str(row.get("date") or "") == latest_date]
            strategy_rows = [
                row
                for row in latest
                if str(row.get("strategy_status") or "") not in {"baseline", "benchmark"}
            ]
            lines.append(f"- Latest completed row date: `{latest_date}`")
            lines.append(
                f"- Distinct recorded sessions: "
                f"`{len({str(row.get('date') or '') for row in mode_rows})}`"
            )
            if strategy_rows:
                best = max(strategy_rows, key=_calendar_daily_return)
                worst = min(strategy_rows, key=_calendar_daily_return)
                drawdown = [
                    _calendar_series_label(row)
                    for row in strategy_rows
                    if _calendar_drawdown(row) < 0
                ]
                lines.append(
                    f"- Best strategy on that row: `{_calendar_series_label(best)}` "
                    f"(`{best.get('daily_return_pct', 'N/A')}`)."
                )
                lines.append(
                    f"- Worst strategy on that row: `{_calendar_series_label(worst)}` "
                    f"(`{worst.get('daily_return_pct', 'N/A')}`)."
                )
                lines.append(
                    "- Strategies in drawdown: " + (", ".join(drawdown) if drawdown else "none")
                )
            else:
                lines.append("- No active strategy rows; only reference policies are present.")
            references = [
                _calendar_series_label(row)
                for row in latest
                if str(row.get("strategy_status") or "") in {"baseline", "benchmark"}
            ]
            lines.append(
                "- Reference policies recorded: "
                + (", ".join(references) if references else "none")
            )
            lines.append("")
        lines.extend(
            [
                "No improving, decaying, or promotion claim is made here; those require "
                "the separately gated forward-evidence evaluation.",
                "",
            ]
        )
        summary = "\n".join(lines)
    (paths.calendar / "calendar_summary.md").write_text(summary + "\n", encoding="utf-8")


def _calendar_daily_return(row: dict[str, object]) -> float:
    try:
        return float(row.get("daily_return_pct") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _calendar_drawdown(row: dict[str, object]) -> float:
    try:
        return float(row.get("drawdown_pct") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _calendar_series_label(row: dict[str, object]) -> str:
    strategy_id = str(row.get("strategy_id") or "unknown")
    strategy_version = str(row.get("strategy_version") or "unknown")
    policy = str(row.get("execution_policy_version") or "unknown")
    return f"{strategy_id}@{strategy_version} [{policy}]"


def _write_daily_report(
    paths: PaperOpsPaths,
    run: PaperRun,
    manifest: DataTruthManifest,
    stats: dict[str, object],
    warnings: tuple[str, ...],
) -> None:
    report_stem = f"{run.mode.value}_{run.run_date}"
    report_path = paths.reports / "daily" / f"{report_stem}.json"
    existing = read_json(report_path, {})
    phases = (
        dict(existing.get("phases", {}))
        if isinstance(existing, dict) and isinstance(existing.get("phases"), dict)
        else {}
    )
    phase_name = str(stats.get("phase") or "latest")
    phases[phase_name] = {"stats": stats, "warnings": list(warnings)}
    payload = {
        "data_snapshot_id": manifest.snapshot_id,
        "date": run.run_date,
        "mode": run.mode.value,
        "provider_status": manifest.validation_status,
        "reconciliation_status": _reconciliation_status(manifest),
        "run_id": run.run_id,
        "phases": phases,
        "stats": stats,
        "warnings": list(warnings),
    }
    write_json(report_path, payload)
    if run.mode is PaperRunMode.FORWARD:
        write_json(paths.reports / "daily" / f"{run.run_date}.json", payload)
    lines = [
        f"# PaperOps Daily Report {run.run_date}",
        "",
        f"- Mode: `{run.mode.value}`",
        f"- Run ID: `{run.run_id}`",
        f"- Data snapshot: `{manifest.snapshot_id}`",
        f"- Provider/reconciliation: `{payload['reconciliation_status']}`",
        f"- Latest stats: `{json.dumps(stats, sort_keys=True)}`",
        "",
        "## Phase history",
        "",
    ]
    lines.extend(
        f"- `{name}`: `{json.dumps(value, sort_keys=True)}`"
        for name, value in sorted(phases.items())
    )
    lines.extend(("", "## Warnings", ""))
    lines.extend(f"- {warning}" for warning in warnings or ("None.",))
    report_markdown = "\n".join(lines) + "\n"
    (paths.reports / "daily" / f"{report_stem}.md").write_text(
        report_markdown,
        encoding="utf-8",
    )
    if run.mode is PaperRunMode.FORWARD:
        (paths.reports / "daily" / f"{run.run_date}.md").write_text(
            report_markdown,
            encoding="utf-8",
        )


def _reconciliation_status(manifest: DataTruthManifest) -> str:
    if manifest.provider_id == "synthetic" or manifest.validation_status == "demo_only":
        return "demo_only"
    return _RECONCILIATION_STATUS_BY_SNAPSHOT.get(
        manifest.snapshot_id,
        "unknown_unverified",
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


def _ensure_run_manifest(
    paths: PaperOpsPaths,
    run: PaperRun,
    *,
    config: PaperOpsConfig,
    data_manifest: DataTruthManifest,
    data_truth_root: Path,
) -> dict[str, object]:
    """Persist one immutable, phase-independent binding for a PaperOps run."""

    if run.data_snapshot_id != data_manifest.snapshot_id:
        raise ValueError("PaperOps run snapshot does not match its DataTruth manifest")
    _ensure_execution_policy_manifest(paths, config)
    data_truth_relative = Path(
        os.path.relpath(data_truth_root.resolve(), paths.root.resolve())
    ).as_posix()
    policy_fingerprint = _execution_policy_fingerprint(config)
    manifest = PaperOpsManifest(
        run_id=run.run_id,
        mode=run.mode,
        run_date=run.run_date,
        data_snapshot_id=run.data_snapshot_id,
        output_artifacts=(),
        warnings=tuple(data_manifest.warnings),
        execution_policy_version=config.execution_policy_version,
        execution_policy_fingerprint=policy_fingerprint,
        universe_id=config.universe_id,
        universe_symbols=config.universe_symbols,
        data_snapshot_content_hash=data_manifest.snapshot_content_hash,
        data_snapshot_manifest_payload_hash=data_manifest.manifest_payload_hash,
        data_snapshot_normalized_hash=data_manifest.normalized_artifact_hash,
        data_snapshot_normalized_path=data_manifest.normalized_artifact_path,
        data_truth_root_relative=data_truth_relative,
    )
    manifest_payload = manifest.to_dict()
    manifest_payload.pop("manifest_payload_hash", None)
    manifest = replace(
        manifest,
        manifest_payload_hash=hashlib.sha256(
            json.dumps(
                manifest_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    )
    proposed = manifest.to_dict()
    manifest_path = paths.manifests / f"{_safe_filename(run.run_id)}.json"
    manifest_lock = manifest_path.with_name(f".{manifest_path.name}.lock")
    with exclusive_file_lock(manifest_lock):
        if manifest_path.exists():
            existing = read_json(manifest_path, None)
            if not isinstance(existing, dict):
                raise ValueError("PaperOps run manifest must be a JSON object")
            if existing != proposed:
                raise ValueError(
                    "PaperOps run manifest conflicts with the immutable same-run binding"
                )
            return dict(existing)
        write_json(manifest_path, proposed)
    return proposed


def _append_events(paths: PaperOpsPaths, events: list[PaperLedgerEvent]) -> None:
    append_jsonl_unique(
        paths.ledger / "paper_ledger.jsonl",
        [event.to_dict() for event in events],
        "event_id",
    )


def _commit_paper_transaction(
    paths: PaperOpsPaths,
    *,
    events: list[PaperLedgerEvent],
    state_updates: dict[Path, object],
) -> None:
    journal_path = paths.state / "paper_transaction_pending.json"
    with exclusive_file_lock(paths.state / ".paper_transaction.lock"):
        _recover_pending_transaction_unlocked(paths, journal_path)
        serialized_updates: dict[str, object] = {}
        for path, payload in state_updates.items():
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(paths.root.resolve())
            except ValueError as exc:
                raise ValueError("PaperOps transaction state path escaped its root") from exc
            serialized_updates[relative.as_posix()] = payload
        event_rows = [event.to_dict() for event in events]
        transaction_id = _paper_transaction_id(event_rows, serialized_updates)
        journal = {
            "events": event_rows,
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": serialized_updates,
            "transaction_id": transaction_id,
        }
        write_json(journal_path, journal)
        _apply_transaction_journal(paths, journal)
        journal_path.unlink(missing_ok=True)


def _recover_pending_transaction(paths: PaperOpsPaths) -> None:
    journal_path = paths.state / "paper_transaction_pending.json"
    with exclusive_file_lock(paths.state / ".paper_transaction.lock"):
        _recover_pending_transaction_unlocked(paths, journal_path)


def _recover_pending_transaction_unlocked(
    paths: PaperOpsPaths,
    journal_path: Path,
) -> None:
    journal = read_json(journal_path, {})
    if not isinstance(journal, dict) or not journal:
        return
    _apply_transaction_journal(paths, journal)
    journal_path.unlink(missing_ok=True)


def _apply_transaction_journal(
    paths: PaperOpsPaths,
    journal: dict[str, object],
) -> None:
    if journal.get("schema_version") != "v2.paper_transaction.v1":
        raise ValueError("PaperOps transaction journal schema is unsupported")
    transaction_id = str(journal.get("transaction_id") or "").strip()
    if not transaction_id:
        raise ValueError("PaperOps transaction journal has no transaction identity")
    event_rows = journal.get("events", [])
    updates = journal.get("state_updates", {})
    if not isinstance(event_rows, list) or not all(
        isinstance(row, dict) and str(row.get("event_id") or "").strip() for row in event_rows
    ):
        raise ValueError("PaperOps transaction journal events are malformed")
    if not isinstance(updates, dict):
        raise ValueError("PaperOps transaction journal state updates are malformed")
    expected_transaction_id = _paper_transaction_id(event_rows, updates)
    if transaction_id != expected_transaction_id:
        raise ValueError("PaperOps transaction journal checksum does not match its payload")
    validated_updates: list[tuple[Path, object]] = []
    for relative_name, payload in sorted(updates.items()):
        if not isinstance(relative_name, str) or not relative_name.strip():
            raise ValueError("PaperOps transaction journal state path is malformed")
        target = (paths.root / relative_name).resolve()
        try:
            target.relative_to(paths.root.resolve())
        except ValueError as exc:
            raise ValueError("PaperOps transaction journal path escaped its root") from exc
        if not _is_allowed_transaction_target(relative_name):
            raise ValueError(
                f"PaperOps transaction journal target is not writer-allowlisted: {relative_name}"
            )
        validated_updates.append((target, payload))
    append_jsonl_unique(
        paths.ledger / "paper_ledger.jsonl",
        [dict(row) for row in event_rows],
        "event_id",
    )
    for target, payload in validated_updates:
        write_json(target, payload)


def _is_allowed_transaction_target(relative_name: str) -> bool:
    """Allow only files emitted by production transaction call sites.

    A pending journal is untrusted recovery input.  Keeping this list explicit
    prevents a checksum-valid journal from becoming a generic root writer.
    """

    normalized = relative_name.replace("\\", "/")
    core = {
        "state/pending_orders.json",
        "state/open_positions.json",
        "state/paper_accounts.json",
        "state/replay_pending_orders.json",
        "state/replay_open_positions.json",
        "state/replay_paper_accounts.json",
        "state/demo_pending_orders.json",
        "state/demo_open_positions.json",
        "state/demo_paper_accounts.json",
    }
    if normalized in core:
        return True
    safe = r"[A-Za-z0-9_.-]+"
    mode = r"(?:forward|replay|demo)"
    patterns = (
        rf"state/shadow/{safe}/{mode}_(?:pending_orders|open_positions|account)\.json",
        rf"exports/shadow_(?:strategy_decisions|picks|order_decisions)_{mode}_\d{{4}}-\d{{2}}-\d{{2}}_{safe}\.json",
        rf"manifests/shadow_{mode}_\d{{4}}-\d{{2}}-\d{{2}}_{safe}\.json",
    )
    return any(re.fullmatch(pattern, normalized) is not None for pattern in patterns)


def _paper_transaction_id(
    event_rows: list[dict[str, object]],
    state_updates: dict[str, object],
) -> str:
    canonical = json.dumps(
        {"events": event_rows, "state_updates": state_updates},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        event_id=stable_id(
            "paper_ops_event",
            run.mode.value,
            run.run_date,
            phase.value,
            event_type,
            entity_id,
        ),
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
    return _config_from_payload(payload)


def _config_from_payload(payload: dict[str, object]) -> PaperOpsConfig:
    raw_symbols = payload.get("universe_symbols", DEFAULT_PAPEROPS_UNIVERSE)
    if not isinstance(raw_symbols, list | tuple):
        raise ValueError("PaperOps universe_symbols must be an array")
    if any(not isinstance(symbol, str) or not symbol.strip() for symbol in raw_symbols):
        raise ValueError("PaperOps universe_symbols entries must be non-blank strings")
    universe_symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in raw_symbols))
    if not universe_symbols:
        raise ValueError("PaperOps universe_symbols must not be empty")
    config = PaperOpsConfig(
        starting_equity=float(payload.get("starting_equity", 100_000.0)),
        risk_per_trade_pct=float(payload.get("risk_per_trade_pct", 0.005)),
        max_daily_loss_pct=float(payload.get("max_daily_loss_pct", 0.015)),
        max_open_risk_pct=float(payload.get("max_open_risk_pct", 0.02)),
        max_gross_exposure_pct=float(payload.get("max_gross_exposure_pct", 1.0)),
        max_concurrent_positions=int(payload.get("max_concurrent_positions", 3)),
        allow_experimental=_strict_config_bool(payload, "allow_experimental", True),
        allow_single_provider_forward=_strict_config_bool(
            payload,
            "allow_single_provider_forward",
            True,
        ),
        min_reward_risk=float(payload.get("min_reward_risk", 1.0)),
        fee_bps=float(payload.get("fee_bps", 1.0)),
        slippage_bps=float(payload.get("slippage_bps", 5.0)),
        execution_policy_version=str(
            payload.get("execution_policy_version") or PAPER_EXECUTION_POLICY_VERSION
        ),
        universe_id=str(payload.get("universe_id", "us_liquid_daily_v1")),
        universe_symbols=universe_symbols,
    )
    _validate_config(config)
    return config


def _strict_config_bool(
    payload: dict[str, object],
    key: str,
    default: bool,
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"PaperOps {key} must be a JSON boolean")
    return value


def _validate_config(config: PaperOpsConfig) -> None:
    positive = {
        "starting_equity": config.starting_equity,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "max_daily_loss_pct": config.max_daily_loss_pct,
        "max_open_risk_pct": config.max_open_risk_pct,
        "max_gross_exposure_pct": config.max_gross_exposure_pct,
        "min_reward_risk": config.min_reward_risk,
    }
    for name, value in positive.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"PaperOps {name} must be a finite positive number")
    for name, value in (
        ("risk_per_trade_pct", config.risk_per_trade_pct),
        ("max_daily_loss_pct", config.max_daily_loss_pct),
        ("max_open_risk_pct", config.max_open_risk_pct),
    ):
        if value > 1:
            raise ValueError(f"PaperOps {name} must be less than or equal to 1")
    if config.max_concurrent_positions < 1:
        raise ValueError("PaperOps max_concurrent_positions must be at least 1")
    for name, value in (("fee_bps", config.fee_bps), ("slippage_bps", config.slippage_bps)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"PaperOps {name} must be finite and non-negative")
    if not config.execution_policy_version.strip():
        raise ValueError("PaperOps execution_policy_version must not be blank")
    if not config.universe_id.strip() or not config.universe_symbols:
        raise ValueError("PaperOps universe identity and symbols must not be blank")


def _ensure_execution_policy_manifest(
    paths: PaperOpsPaths,
    active_config: PaperOpsConfig,
) -> None:
    manifest_path = paths.state / "execution_policy_manifest.json"
    raw_manifest = read_json(manifest_path, {})
    has_forward_evidence = _has_forward_evidence(paths)
    if not isinstance(raw_manifest, dict) or not raw_manifest:
        if has_forward_evidence:
            raise ValueError(
                "PaperOps forward evidence has no immutable execution-policy manifest; "
                "migrate it explicitly before running"
            )
        raw_manifest = {
            "active_execution_policy_version": active_config.execution_policy_version,
            "policies": {},
            "schema_version": "v2.paper_execution_policy_manifest.v1",
        }
    policies = raw_manifest.get("policies")
    if not isinstance(policies, dict):
        raise ValueError("PaperOps execution-policy manifest policies must be an object")
    active_version = active_config.execution_policy_version
    fingerprint_payload = _execution_policy_fingerprint_payload(active_config)
    fingerprint = _execution_policy_fingerprint(active_config)
    existing_policy = policies.get(active_version)
    if active_version in policies and not isinstance(existing_policy, dict):
        raise ValueError(f"PaperOps execution policy {active_version} manifest entry is malformed")
    if isinstance(existing_policy, dict):
        if str(existing_policy.get("fingerprint") or "") != fingerprint:
            raise ValueError(
                "PaperOps execution-affecting configuration drifted under the same "
                "execution_policy_version; assign a new explicit policy version"
            )
        _ensure_registration_coverage(
            existing_policy,
            artifact=f"execution policy {active_version}",
        )
    else:
        previous_active = str(raw_manifest.get("active_execution_policy_version") or "")
        if previous_active == active_version and has_forward_evidence:
            raise ValueError(
                "PaperOps active execution-policy lineage is missing despite retained "
                "forward evidence; migrate it explicitly before running"
            )
        live_exposure = [
            *_dict_list(paths.state / "pending_orders.json"),
            *_dict_list(paths.state / "open_positions.json"),
        ]
        if previous_active and previous_active != active_version and live_exposure:
            raise ValueError(
                "PaperOps execution policy cannot roll over with live forward exposure"
            )
        registered_at = datetime.now(timezone.utc)
        policies[active_version] = {
            "activation_policy": _ACTIVATION_POLICY_NEXT_SESSION,
            "configuration": fingerprint_payload,
            "fingerprint": fingerprint,
            "registered_at": registered_at.isoformat(),
            "coverage_inception_date": next_session_after_registration(registered_at).isoformat(),
        }
    previous_active = str(raw_manifest.get("active_execution_policy_version") or "")
    if previous_active and previous_active != active_version and active_version in policies:
        prior = policies.get(active_version)
        if isinstance(prior, dict) and str(prior.get("fingerprint") or "") != fingerprint:
            raise ValueError("PaperOps cannot reactivate a policy with different semantics")
    raw_manifest["active_execution_policy_version"] = active_version
    raw_manifest["policies"] = policies
    raw_manifest["schema_version"] = "v2.paper_execution_policy_manifest.v1"
    write_json(manifest_path, raw_manifest)


def _ensure_strategy_semantics_manifest(
    paths: PaperOpsPaths,
    strategies: tuple[StrategySpec, ...],
) -> dict[tuple[str, str], str]:
    manifest_path = paths.state / "strategy_semantics_manifest.json"
    raw_manifest = read_json(manifest_path, {})
    has_forward_evidence = _has_forward_evidence(paths)
    if not isinstance(raw_manifest, dict) or not raw_manifest:
        if has_forward_evidence:
            raise ValueError(
                "PaperOps forward evidence has no immutable strategy-semantics "
                "manifest; migrate it explicitly before running"
            )
        raw_manifest = {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {},
        }
    stored = raw_manifest.get("strategies")
    if not isinstance(stored, dict):
        raise ValueError("PaperOps strategy semantics manifest must contain an object")
    fingerprints: dict[tuple[str, str], str] = {}
    prior_registry = read_json(paths.state / "strategy_registry.json", [])
    prior_registry_keys = (
        {
            f"{row.get('strategy_id')}@{row.get('strategy_version')}"
            for row in prior_registry
            if isinstance(row, dict)
            and str(row.get("strategy_id") or "")
            and str(row.get("strategy_version") or "")
        }
        if isinstance(prior_registry, list)
        else set()
    )
    for strategy in strategies:
        key = f"{strategy.strategy_id}@{strategy.version}"
        configuration = _strategy_semantics_payload(strategy)
        fingerprint = _strategy_semantics_fingerprint(strategy)
        existing = stored.get(key)
        if key in stored and not isinstance(existing, dict):
            raise ValueError(f"PaperOps strategy semantics entry is malformed for {key}")
        if isinstance(existing, dict):
            if str(existing.get("fingerprint") or "") != fingerprint:
                raise ValueError(
                    "PaperOps strategy semantics changed under the same strategy version; "
                    f"assign a new version for {key}"
                )
            _ensure_registration_coverage(existing, artifact=f"strategy {key}")
        else:
            if key in prior_registry_keys and has_forward_evidence:
                raise ValueError(
                    "PaperOps registered strategy semantics lineage is missing despite "
                    f"retained forward evidence for {key}"
                )
            registered_at = datetime.now(timezone.utc)
            stored[key] = {
                "activation_policy": _ACTIVATION_POLICY_NEXT_SESSION,
                "configuration": configuration,
                "fingerprint": fingerprint,
                "registered_at": registered_at.isoformat(),
                "coverage_inception_date": next_session_after_registration(
                    registered_at
                ).isoformat(),
            }
        fingerprints[(strategy.strategy_id, strategy.version)] = fingerprint
    raw_manifest["schema_version"] = "v2.strategy_semantics_manifest.v1"
    raw_manifest["strategies"] = stored
    write_json(manifest_path, raw_manifest)
    return fingerprints


def _ensure_registration_coverage(entry: dict[str, object], *, artifact: str) -> None:
    registered_raw = str(entry.get("registered_at") or "").strip()
    try:
        registered_at = datetime.fromisoformat(registered_raw)
    except ValueError as exc:
        raise ValueError(f"PaperOps {artifact} registered_at is invalid") from exc
    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        raise ValueError(f"PaperOps {artifact} registered_at must include a timezone")
    activation_policy = str(
        entry.get("activation_policy") or _ACTIVATION_POLICY_FIRST_ELIGIBLE
    ).strip()
    try:
        expected = registration_coverage_inception_date(
            registered_at,
            activation_policy,
        ).isoformat()
    except ValueError as exc:
        raise ValueError(f"PaperOps {artifact} activation_policy is unsupported") from exc
    stored_inception = str(entry.get("coverage_inception_date") or "").strip()
    if stored_inception and stored_inception != expected:
        raise ValueError(
            f"PaperOps {artifact} coverage_inception_date conflicts with registered_at"
        )
    entry["coverage_inception_date"] = expected


def _strategy_coverage_inception(
    paths: PaperOpsPaths,
    *,
    strategy_id: str,
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> date:
    """Resolve exact-series forward inception from immutable lineage manifests."""

    semantics_manifest = read_json(paths.state / "strategy_semantics_manifest.json", {})
    policy_manifest = read_json(paths.state / "execution_policy_manifest.json", {})
    if not isinstance(semantics_manifest, dict) or not isinstance(policy_manifest, dict):
        raise ValueError("PaperOps immutable registration manifests are unavailable")
    strategies = semantics_manifest.get("strategies")
    policies = policy_manifest.get("policies")
    if not isinstance(strategies, dict) or not isinstance(policies, dict):
        raise ValueError("PaperOps immutable registration manifests are malformed")
    strategy_key = f"{strategy_id}@{strategy_version}"
    strategy_entry = strategies.get(strategy_key)
    policy_entry = policies.get(execution_policy_version)
    if not isinstance(strategy_entry, dict):
        raise ValueError(f"PaperOps strategy registration is missing for {strategy_key}")
    if not isinstance(policy_entry, dict):
        raise ValueError(
            f"PaperOps execution-policy registration is missing for {execution_policy_version}"
        )
    if str(strategy_entry.get("fingerprint") or "") != strategy_semantics_fingerprint:
        raise ValueError(f"PaperOps strategy registration fingerprint conflicts for {strategy_key}")
    strategy_copy = dict(strategy_entry)
    policy_copy = dict(policy_entry)
    _ensure_registration_coverage(strategy_copy, artifact=f"strategy {strategy_key}")
    _ensure_registration_coverage(
        policy_copy,
        artifact=f"execution policy {execution_policy_version}",
    )
    return max(
        date.fromisoformat(str(strategy_copy["coverage_inception_date"])),
        date.fromisoformat(str(policy_copy["coverage_inception_date"])),
    )


def _series_is_eligible_for_run(
    paths: PaperOpsPaths,
    *,
    run_date: date,
    mode: PaperRunMode,
    strategy_id: str,
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> bool:
    """Keep replay counterfactual while forward evidence starts at inception."""

    if mode is not PaperRunMode.FORWARD:
        return True
    return run_date >= _strategy_coverage_inception(
        paths,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        execution_policy_version=execution_policy_version,
        strategy_semantics_fingerprint=strategy_semantics_fingerprint,
    )


def _has_forward_evidence(paths: PaperOpsPaths) -> bool:
    """Return whether immutable forward history or live exposure already exists."""

    if any(
        str(event.get("mode") or "") == PaperRunMode.FORWARD.value
        for event in read_jsonl(paths.ledger / "paper_ledger.jsonl")
    ):
        return True
    if any(
        str(row.get("mode") or "") == PaperRunMode.FORWARD.value
        for row in _read_calendar_rows(paths)
    ):
        return True
    calendar_json = read_json(paths.calendar / "strategy_daily_returns.json", [])
    if isinstance(calendar_json, list) and any(
        isinstance(row, dict) and str(row.get("mode") or "") == PaperRunMode.FORWARD.value
        for row in calendar_json
    ):
        return True
    for manifest_path in paths.manifests.glob("*.json"):
        manifest = read_json(manifest_path, {})
        if (
            isinstance(manifest, dict)
            and str(manifest.get("mode") or "") == PaperRunMode.FORWARD.value
        ):
            return True
    account_payload = read_json(paths.state / "paper_accounts.json", {})
    account_rows = account_payload.get("accounts", []) if isinstance(account_payload, dict) else []
    if isinstance(account_rows, list) and any(
        isinstance(row, dict)
        and (
            float(row.get("current_equity") or 0.0) != float(row.get("starting_equity") or 0.0)
            or float(row.get("realized_pnl") or 0.0) != 0.0
            or float(row.get("unrealized_pnl") or 0.0) != 0.0
        )
        for row in account_rows
    ):
        return True
    return bool(
        _dict_list(paths.state / "pending_orders.json")
        or _dict_list(paths.state / "open_positions.json")
    )


def _strategy_semantics_payload(strategy: StrategySpec) -> dict[str, object]:
    return strategy_semantics_payload(strategy)


def _strategy_semantics_fingerprint(strategy: StrategySpec) -> str:
    return strategy_semantics_fingerprint(strategy)


def _governance_block_reason(
    paths: PaperOpsPaths,
    strategy: StrategySpec,
    config: PaperOpsConfig,
) -> str | None:
    overlay_path = paths.state / "strategy_governance_overlay.json"
    if not overlay_path.exists():
        return None
    payload = read_json(overlay_path, {})
    if not isinstance(payload, dict):
        raise ValueError("PaperOps strategy governance overlay must be an object")
    if payload.get("schema_version") != "v2.strategy_governance_overlay.v1":
        raise ValueError("PaperOps strategy governance overlay schema is unsupported")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("PaperOps strategy governance overlay entries must be an array")
    seen: set[tuple[str, str, str, str]] = set()
    active_triple = (
        strategy.strategy_id,
        strategy.version,
        config.execution_policy_version,
    )
    active_fingerprint = _strategy_semantics_fingerprint(strategy)
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("PaperOps strategy governance overlay row is malformed")
        row_triple = (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
        )
        row_fingerprint = str(row.get("strategy_semantics_fingerprint") or "")
        identity = (*row_triple, row_fingerprint)
        if not all(identity):
            raise ValueError("PaperOps strategy governance identity is incomplete")
        if identity in seen:
            raise ValueError("PaperOps strategy governance identity is duplicated")
        seen.add(identity)
        if not isinstance(row.get("allow_entries"), bool):
            raise ValueError("PaperOps strategy governance allow_entries must be boolean")
        if row.get("allow_entries") is not False:
            raise ValueError("PaperOps governance cannot automatically re-enable entries")
        if row_triple == active_triple and row_fingerprint != active_fingerprint:
            raise ValueError(
                "PaperOps strategy governance fingerprint conflicts with current semantics"
            )
        if row_triple == active_triple and row_fingerprint == active_fingerprint:
            return str(row.get("reason") or "governance_pause")
    return None


def _execution_policy_fingerprint(config: PaperOpsConfig) -> str:
    return hashlib.sha256(
        json.dumps(
            _execution_policy_fingerprint_payload(config),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _execution_policy_fingerprint_payload(config: PaperOpsConfig) -> dict[str, object]:
    return {
        "allow_experimental": config.allow_experimental,
        "allow_single_provider_forward": config.allow_single_provider_forward,
        "engine_policy_implementation": PAPER_EXECUTION_POLICY_VERSION,
        "fee_bps": config.fee_bps,
        "max_concurrent_positions": config.max_concurrent_positions,
        "max_daily_loss_pct": config.max_daily_loss_pct,
        "max_gross_exposure_pct": config.max_gross_exposure_pct,
        "max_open_risk_pct": config.max_open_risk_pct,
        "min_reward_risk": config.min_reward_risk,
        "paper_timeout_days": PAPER_TIMEOUT_DAYS,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "slippage_bps": config.slippage_bps,
        "starting_equity": config.starting_equity,
        "universe_id": config.universe_id,
        "universe_symbols": list(config.universe_symbols),
    }


def _assert_strategy_registry_upgrade_safe(
    paths: PaperOpsPaths,
    stored_registry: object,
    active_registry: list[dict[str, object]],
) -> None:
    if not isinstance(stored_registry, list) or not stored_registry:
        return
    stored_series = {
        str(row.get("strategy_id")): (
            str(row.get("strategy_version")),
            str(row.get("strategy_semantics_fingerprint") or ""),
        )
        for row in stored_registry
        if isinstance(row, dict) and row.get("strategy_id")
    }
    active_series = {
        str(row.get("strategy_id")): (
            str(row.get("strategy_version")),
            str(row.get("strategy_semantics_fingerprint") or ""),
        )
        for row in active_registry
        if row.get("strategy_id")
    }
    semantic_drift = {
        strategy_id
        for strategy_id, (active_version, active_fingerprint) in active_series.items()
        if strategy_id in stored_series
        and stored_series[strategy_id][0] == active_version
        and stored_series[strategy_id][1] != active_fingerprint
    }
    if semantic_drift:
        raise ValueError(
            "PaperOps stored strategy semantics do not match the current code under "
            "the same version; restore the manifest/registry or assign a new version: "
            + ", ".join(sorted(semantic_drift))
        )
    changed = {
        strategy_id
        for strategy_id, (version, _fingerprint) in active_series.items()
        if strategy_id in stored_series and stored_series[strategy_id][0] != version
    }
    if not changed:
        return
    live_rows = [
        *_dict_list(paths.state / "pending_orders.json"),
        *_dict_list(paths.state / "open_positions.json"),
    ]
    if any(str(row.get("strategy_id") or "") in changed for row in live_rows):
        raise ValueError(
            "PaperOps strategy version changed with live forward exposure; "
            "close or explicitly roll over the prior version first"
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
            execution_policy_version=active_config.execution_policy_version,
            strategy_semantics_fingerprint=str(
                row.get("strategy_semantics_fingerprint") or "unknown"
            ),
        ).to_dict()
        for row in _strategy_registry(paths)
    ]
    return {"accounts": accounts, "schema_version": "v2.paper_account_state.v3"}


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
    active_config = _config(paths)
    active_registry = {
        str(row["strategy_id"]): (
            str(row["strategy_version"]),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        )
        for row in _strategy_registry(paths)
    }
    rows_by_key = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or "legacy_unspecified"),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        ): row
        for row in rows
        if isinstance(row, dict)
    }
    changed = False
    accounts: dict[str, StrategyPaperAccount] = {}
    for strategy_id, registry_series in active_registry.items():
        strategy_version, strategy_semantics_fingerprint = registry_series
        key = (
            strategy_id,
            strategy_version,
            active_config.execution_policy_version,
            strategy_semantics_fingerprint,
        )
        row = rows_by_key.get(key)
        if row is None:
            row = StrategyPaperAccount(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                starting_equity=active_config.starting_equity,
                current_equity=active_config.starting_equity,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                execution_policy_version=active_config.execution_policy_version,
                strategy_semantics_fingerprint=strategy_semantics_fingerprint,
            ).to_dict()
            rows.append(row)
            changed = True
        account = StrategyPaperAccount(
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            starting_equity=float(row["starting_equity"]),
            current_equity=float(row["current_equity"]),
            realized_pnl=float(row["realized_pnl"]),
            unrealized_pnl=float(row["unrealized_pnl"]),
            execution_policy_version=str(
                row.get("execution_policy_version") or "legacy_unspecified"
            ),
            strategy_semantics_fingerprint=str(
                row.get("strategy_semantics_fingerprint") or "unknown"
            ),
        )
        accounts[account.strategy_id] = account
    if changed:
        write_json(
            account_path,
            {"accounts": rows, "schema_version": "v2.paper_account_state.v3"},
        )
    return accounts


def _write_accounts(
    paths: PaperOpsPaths,
    mode: PaperRunMode,
    accounts: dict[str, StrategyPaperAccount],
) -> None:
    write_json(
        _paper_accounts_path(paths, mode),
        _account_state_payload(paths, mode, accounts),
    )


def _account_state_payload(
    paths: PaperOpsPaths,
    mode: PaperRunMode,
    accounts: dict[str, StrategyPaperAccount],
) -> dict[str, object]:
    account_path = _paper_accounts_path(paths, mode)
    payload = read_json(account_path, {})
    existing = (
        [row for row in payload.get("accounts", []) if isinstance(row, dict)]
        if isinstance(payload, dict)
        else []
    )
    by_key = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or "legacy_unspecified"),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        ): row
        for row in existing
    }
    for account in accounts.values():
        by_key[
            (
                account.strategy_id,
                account.strategy_version,
                account.execution_policy_version,
                account.strategy_semantics_fingerprint,
            )
        ] = account.to_dict()
    state = PaperAccountState(
        accounts=tuple(
            StrategyPaperAccount(
                strategy_id=str(row["strategy_id"]),
                strategy_version=str(row["strategy_version"]),
                starting_equity=float(row["starting_equity"]),
                current_equity=float(row["current_equity"]),
                realized_pnl=float(row["realized_pnl"]),
                unrealized_pnl=float(row["unrealized_pnl"]),
                execution_policy_version=str(
                    row.get("execution_policy_version") or "legacy_unspecified"
                ),
                strategy_semantics_fingerprint=str(
                    row.get("strategy_semantics_fingerprint") or "unknown"
                ),
            )
            for _, row in sorted(by_key.items())
        )
    )
    return state.to_dict()


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
                warnings.append("earliest_fill_date repaired from legacy calendar-day logic")
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
    ledger_path = paths.ledger / "paper_ledger.jsonl"
    with exclusive_file_lock(jsonl_lock_path(ledger_path)):
        retained_events = [
            event for event in read_jsonl(ledger_path) if str(event.get("mode") or "") != mode.value
        ]
        write_jsonl(ledger_path, retained_events)
    retained_calendar_rows = [
        row for row in _read_calendar_rows(paths) if str(row.get("mode") or "") != mode.value
    ]
    write_csv(
        paths.calendar / "strategy_daily_returns.csv",
        retained_calendar_rows,
        CALENDAR_FIELDNAMES,
    )
    write_json(paths.calendar / "strategy_daily_returns.json", retained_calendar_rows)
    _write_calendar_matrix(paths, retained_calendar_rows)
    _write_monthly_returns(paths, retained_calendar_rows)
    _write_equity_and_drawdown(paths, retained_calendar_rows)
    _write_calendar_summary(paths, retained_calendar_rows)


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
        execution_policy_version=str(row.get("execution_policy_version") or "legacy_unspecified"),
        strategy_semantics_fingerprint=str(row.get("strategy_semantics_fingerprint") or "unknown"),
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
        execution_policy_version=str(row.get("execution_policy_version") or "legacy_unspecified"),
        strategy_semantics_fingerprint=str(row.get("strategy_semantics_fingerprint") or "unknown"),
        entry_fee=float(row.get("entry_fee", 0.0)),
        realized_pnl=float(row.get("realized_pnl", 0.0)),
        unrealized_pnl=float(row.get("unrealized_pnl", 0.0)),
    )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
