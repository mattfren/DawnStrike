"""Read-only Interface Apex view models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MissionModel:
    headline: str
    subheadline: str
    status: str
    latest_run_time: str
    next_run_time: str
    day_return: str
    cumulative_return: str
    paper_trades_today: int
    open_paper_trades: int
    top_strategy: str
    top_warning: str
    latest_lesson: str
    next_action: str


@dataclass(frozen=True)
class CalendarDayTile:
    date: str
    daily_return_pct: str
    cumulative_return_pct: str
    trade_count: int
    no_trade_marker: bool
    warning_marker: bool
    learning_marker: bool
    day_story_link: str
    tone: str


@dataclass(frozen=True)
class CalendarMonth:
    month: str
    monthly_return_pct: str
    cumulative_return_pct: str
    best_day: str
    worst_day: str
    win_days: int
    loss_days: int
    no_trade_days: int
    total_trades: int
    previous_month: str
    next_month: str
    day_tiles: list[CalendarDayTile] = field(default_factory=list)


@dataclass(frozen=True)
class CalendarModel:
    months: list[CalendarMonth]
    current_month: str
    day_tiles: list[CalendarDayTile]


@dataclass(frozen=True)
class TradeModel:
    trade_id: str
    date: str
    symbol: str
    strategy: str
    interval: str
    entry_time: str
    exit_time: str
    hold_minutes: str
    direction: str
    entry_price: str
    exit_price: str
    stop: str
    target: str
    exit_reason: str
    r_multiple: str
    pnl: str
    evidence_type: str
    official_or_shadow: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DayModel:
    date: str
    headline: str
    plain_english_summary: str
    daily_return: str
    cumulative_return: str
    trades: list[TradeModel]
    strategies_evaluated: list[str]
    no_picks_reasons: list[str]
    warnings: list[str]
    learning_note: str
    market_masters_note: str
    evidence_quality: str
    what_to_watch_tomorrow: str


@dataclass(frozen=True)
class StrategyModel:
    strategy_id: str
    name: str
    lane: str
    status: str
    trade_count: int
    win_rate: str
    average_r: str
    expectancy: str
    profit_factor: str
    drawdown: str
    robustness_score: str
    slippage_status: str
    oos_status: str
    validation_progress: str
    warnings: list[str]
    detail_link: str
    trade_examples: list[TradeModel] = field(default_factory=list)
    best_conditions: list[str] = field(default_factory=list)
    worst_conditions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IntelligenceModel:
    learning_foundry_status: str
    latest_lesson: str
    regime: str
    market_masters_status: str
    methodologies: list[str]
    primitives: list[str]
    challengers: list[str]
    promotion_status: str
    shadow_only_count: int
    validation_blocked_reason: str


@dataclass(frozen=True)
class SystemModel:
    scheduled_tasks: list[dict[str, Any]]
    provider_status: str
    telegram_status: str
    sentinel_status: str
    watchdog_status: str
    data_quality_status: str
    evidence_chain_status: str
    live_trading_disabled: bool
    secrets_safe: bool
    warnings: list[str]


@dataclass(frozen=True)
class NoPicksModel:
    date: str
    headline: str
    accepted_count: int
    blocked_count: int
    watch_count: int
    no_setup_count: int
    top_reasons: list[str]
    near_setups: list[str]
    strategies_blocked: list[str]
    data_quality_blockers: list[str]
    riskhub_blockers: list[str]
    what_would_change: list[str]
    why_no_trade_is_valid: str


@dataclass(frozen=True)
class InterfaceApexModel:
    mission: MissionModel
    calendar: CalendarModel
    days: list[DayModel]
    strategies: list[StrategyModel]
    trades: list[TradeModel]
    intelligence: IntelligenceModel
    system: SystemModel
    no_picks: NoPicksModel
    source_refs: list[dict[str, Any]]
    warnings: list[str]
    build_seed: dict[str, Any]


def to_plain(value: Any) -> Any:
    """Convert dataclass trees into JSON-safe dictionaries."""

    if hasattr(value, "__dataclass_fields__"):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    return value
