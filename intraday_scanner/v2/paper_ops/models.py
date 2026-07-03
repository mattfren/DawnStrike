"""Typed PaperOps v1 domain models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, cast


class PaperDataMode(str, Enum):
    PUBLIC_SINGLE_PROVIDER = "public_single_provider"
    RECONCILED = "reconciled"
    SYNTHETIC = "synthetic"


class PaperRunMode(str, Enum):
    FORWARD = "forward"
    REPLAY = "replay"
    DEMO = "demo"


class PaperJobPhase(str, Enum):
    INIT = "init"
    PREFLIGHT = "preflight"
    SCAN = "scan"
    ENTER = "enter"
    CHECK = "check"
    CLOSE = "close"
    CALENDAR = "calendar"
    RECONCILE = "reconcile"
    REPORT = "report"


class PaperPickDecision(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class PaperOrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class PaperPositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class PaperCloseReason(str, Enum):
    STOP = "stop"
    TARGET = "target"
    INVALIDATION = "invalidation"
    TIMEOUT = "timeout"
    EOD_FLAT = "eod_flat"
    FORCED = "forced"
    MARK_TO_MARKET = "mark_to_market"


@dataclass(frozen=True)
class PaperOpsConfig:
    starting_equity: float = 100_000.0
    risk_per_trade_pct: float = 0.005
    max_daily_loss_pct: float = 0.015
    max_open_risk_pct: float = 0.02
    max_concurrent_positions: int = 3
    allow_experimental: bool = True
    allow_single_provider_forward: bool = True
    min_reward_risk: float = 1.0
    fee_bps: float = 1.0
    slippage_bps: float = 5.0
    schema_version: str = "v2.paper_ops_config.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperRun:
    run_id: str
    mode: PaperRunMode
    run_date: str
    data_snapshot_id: str
    created_at: str
    schema_version: str = "v2.paper_run.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperJobLog:
    log_id: str
    run_id: str
    phase: PaperJobPhase
    status: str
    message: str
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.paper_job_log.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperStrategyConfig:
    strategy_id: str
    strategy_version: str
    strategy_status: str
    paper_status: str
    allow_entries: bool
    risk_per_trade_pct: float
    max_concurrent_positions: int
    schema_version: str = "v2.paper_strategy_config.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperPick:
    pick_id: str
    run_id: str
    mode: PaperRunMode
    trade_date: str
    strategy_id: str
    strategy_version: str
    strategy_status: str
    symbol: str
    signal_time: str
    direction: str
    setup_score: float
    entry_reference: float
    stop: float | None
    target: float | None
    risk_per_unit: float | None
    reward_per_unit: float | None
    reward_risk: float | None
    decision: PaperPickDecision
    reason: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.paper_pick.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    pick_id: str
    run_id: str
    mode: PaperRunMode
    trade_date: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    order_status: PaperOrderStatus
    expected_fill_rule: str
    signal_time: str
    earliest_fill_date: str
    entry: float
    stop: float
    target: float | None
    risk_per_unit: float
    reward_per_unit: float | None
    reward_risk: float | None
    risk_budget: float
    quantity: int
    notional_exposure: float
    max_loss_estimate: float
    strategy_equity_basis: float
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.paper_order.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    order_id: str
    run_id: str
    mode: PaperRunMode
    strategy_id: str
    symbol: str
    fill_time: str
    fill_price: float
    quantity: int
    fee: float
    slippage: float
    schema_version: str = "v2.paper_fill.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    order_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    status: PaperPositionStatus
    opened_at: str
    quantity: int
    entry_price: float
    stop: float
    target: float | None
    last_mark_price: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    schema_version: str = "v2.paper_position.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperClose:
    close_id: str
    position_id: str
    run_id: str
    mode: PaperRunMode
    strategy_id: str
    symbol: str
    close_time: str
    close_price: float
    close_reason: PaperCloseReason
    gross_pnl: float
    net_pnl: float
    r_multiple: float
    fee: float
    slippage: float
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.paper_close.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperTrade:
    trade_id: str
    order_id: str
    fill_id: str
    close_id: str
    strategy_id: str
    symbol: str
    net_pnl: float
    r_multiple: float
    schema_version: str = "v2.paper_trade.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperLedgerEvent:
    event_id: str
    event_type: str
    run_id: str
    mode: PaperRunMode
    trade_date: str
    strategy_id: str | None
    symbol: str | None
    payload: dict[str, object]
    schema_version: str = "v2.paper_ledger_event.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StrategyPaperAccount:
    strategy_id: str
    strategy_version: str
    starting_equity: float
    current_equity: float
    realized_pnl: float
    unrealized_pnl: float
    schema_version: str = "v2.strategy_paper_account.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperAccountState:
    accounts: tuple[StrategyPaperAccount, ...]
    schema_version: str = "v2.paper_account_state.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class DailyStrategyPerformance:
    date: str
    mode: PaperRunMode
    strategy_id: str
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    daily_return_pct: float
    schema_version: str = "v2.daily_strategy_performance.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class StrategyCalendarRow:
    date: str
    mode: PaperRunMode
    strategy_id: str
    strategy_version: str
    strategy_status: str
    data_snapshot_id: str
    starting_equity: float
    ending_equity: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    daily_return_pct: float
    cumulative_return_pct: float
    drawdown_pct: float
    trades_opened: int
    trades_closed: int
    pending_orders: int
    open_positions: int
    wins: int
    losses: int
    flats: int
    average_r: float
    expectancy_r: float
    exposure_pct: float
    fees_paid: float
    slippage_estimate: float
    warnings: tuple[str, ...]
    run_id: str
    schema_version: str = "v2.strategy_calendar_row.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class StrategyCalendarMatrix:
    rows: tuple[dict[str, object], ...]
    schema_version: str = "v2.strategy_calendar_matrix.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperOpsManifest:
    run_id: str
    mode: PaperRunMode
    run_date: str
    data_snapshot_id: str
    output_artifacts: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.paper_ops_manifest.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperOpsWarning:
    warning_id: str
    severity: str
    message: str
    schema_version: str = "v2.paper_ops_warning.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


@dataclass(frozen=True)
class PaperOpsReconciliationReport:
    report_id: str
    run_id: str
    status: str
    duplicate_event_ids: tuple[str, ...]
    orphan_fills: tuple[str, ...]
    orphan_closes: tuple[str, ...]
    calendar_mismatches: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.paper_ops_reconciliation.v1"

    def to_dict(self) -> dict[str, object]:
        return _plain(self)


def stable_id(*parts: object) -> str:
    return ":".join(str(part).replace(" ", "_") for part in parts if part is not None)


def stable_json(payload: object) -> str:
    return json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"))


def _plain(value: object) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(cast(Any, value)).items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value
