"""Story view models for Command Center X2.

The models are presentation contracts only. They represent already-produced
Dawnstrike artifacts without recalculating strategy signals or mutating state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


@dataclass(frozen=True)
class TrustBoundary:
    label: str
    status: str
    explanation: str


@dataclass(frozen=True)
class TopMetric:
    label: str
    value: str
    context: str
    tone: str = "neutral"


@dataclass(frozen=True)
class SourceRef:
    path: str
    exists: bool
    kind: str


@dataclass(frozen=True)
class PaperTradeStoryModel:
    trade_id: str
    date: str
    symbol: str
    strategy_id: str
    direction: str
    state: str
    entry: str
    stop: str
    target: str
    fill_price: str
    close_price: str
    realized_pnl: str
    unrealized_pnl: str
    r_multiple: str
    reason: str
    evidence_source: str
    filltruth_certainty: str
    commitbridge_status: str
    timeline_events: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class StrategyStoryModel:
    strategy_id: str
    strategy_name: str
    role: str
    status: str
    daily_return_pct: str
    cumulative_return_pct: str
    win_rate: str
    average_r: str
    expectancy: str
    drawdown: str
    trade_count: int
    forward_days: int
    validation_progress: str
    latest_signal_state: str
    latest_paper_state: str
    latest_learning_notes: str
    evidence_quality: str
    warnings: list[str] = field(default_factory=list)
    daily_series: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class NoPicksStoryModel:
    date: str
    headline: str
    accepted_count: int
    blocked_count: int
    watch_count: int
    no_setup_count: int
    top_reasons: list[str]
    strategies_blocked: list[str]
    data_quality_blockers: list[str]
    riskhub_blockers: list[str]
    near_setups: list[str]
    what_would_change: list[str]
    why_no_trade_is_valid: str


@dataclass(frozen=True)
class AutomationStoryModel:
    task_statuses: list[dict[str, Any]]
    next_runs: list[dict[str, str]]
    missed_runs: list[dict[str, str]]
    latest_scheduler_status: str
    latest_watchdog_status: str
    telegram_readiness: str
    autonomous_runner_status: str
    no_overlap_status: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DayStoryModel:
    date: str
    headline: str
    market_context: str
    run_status: str
    provider_status: str
    picks_summary: dict[str, Any]
    no_picks_reasons: list[str]
    paper_trades: list[PaperTradeStoryModel]
    paper_orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    closes: list[dict[str, Any]]
    open_positions: list[dict[str, Any]]
    strategy_returns: list[StrategyStoryModel]
    cumulative_returns: dict[str, str]
    riskhub_summary: str
    filltruth_summary: str
    commitbridge_summary: str
    learning_foundry_lesson: str
    market_masters_lesson: str
    telegram_summary: str
    warnings: list[str]
    what_to_watch_next: list[str]
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True)
class CalendarDayModel:
    date: str
    daily_return_pct: str
    cumulative_return_pct: str
    trade_count: int
    warning_count: int
    state: str
    has_learning: bool
    has_market_masters: bool
    href: str
    tone: str


@dataclass(frozen=True)
class MonthCalendarModel:
    month: str
    calendar_days: list[CalendarDayModel]
    cumulative_return_pct: str
    monthly_return_pct: str
    best_day: str
    worst_day: str
    green_days: int
    red_days: int
    flat_days: int
    no_trade_days: int
    warning_days: int
    previous_month: str
    next_month: str
    source_policy: str


@dataclass(frozen=True)
class AppStoryModel:
    generated_at: str
    latest_run_date: str
    overall_status: str
    alert_level: str
    headline: str
    subheadline: str
    top_metrics: list[TopMetric]
    trust_boundaries: list[TrustBoundary]
    command_center_paths: dict[str, str]
    source_refs: list[SourceRef]
    warnings: list[str]


@dataclass(frozen=True)
class CommandCenterX2StoryBundle:
    app: AppStoryModel
    months: list[MonthCalendarModel]
    days: list[DayStoryModel]
    strategies: list[StrategyStoryModel]
    no_picks: NoPicksStoryModel
    automation: AutomationStoryModel
    learning_cards: list[dict[str, Any]]
    market_masters_cards: list[dict[str, Any]]
    reports: list[dict[str, str]]
    system_flow: list[dict[str, str]]
    calendar_audit: dict[str, Any]


def to_plain(value: Any) -> Any:
    """Convert nested dataclass models to JSON-friendly dictionaries."""
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}  # type: ignore[arg-type]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_plain(item) for item in value]
    return value
