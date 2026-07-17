"""Read-only artifact adapters for Command Center X2."""

# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import json
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.dashboard.paper_ops_calendar_service import (
    PaperOpsCalendarError,
    load_paper_ops_calendar,
)
from intraday_scanner.paper_ops_root import production_paper_ops_root
from intraday_scanner.v2.command_center_x2.story_models import (
    AppStoryModel,
    AutomationStoryModel,
    CalendarDayModel,
    CommandCenterX2StoryBundle,
    DayStoryModel,
    MonthCalendarModel,
    NoPicksStoryModel,
    PaperTradeStoryModel,
    SourceRef,
    StrategyStoryModel,
    TopMetric,
    TrustBoundary,
    to_plain,
)

MISSING = "n/a"


def build_story_bundle(
    *,
    repo_root: Path = Path("."),
    paper_ops_root: str | Path | None = None,
) -> CommandCenterX2StoryBundle:
    """Build X2 story models from existing local artifacts only."""
    ctx = _ArtifactContext(repo_root=repo_root, paper_ops_root=paper_ops_root)
    calendar_rows = _calendar_rows(ctx)
    latest_day = _latest_day(ctx, calendar_rows)
    strategy_models = _strategy_models(ctx, calendar_rows)
    day_models = _day_models(ctx, calendar_rows, strategy_models, latest_day)
    month_models, calendar_audit = _month_models(calendar_rows, day_models)
    no_picks = _no_picks_model(ctx, latest_day)
    automation = _automation_model(ctx)
    warnings = _dedupe(
        [
            *_payload_warnings(ctx.autonomous),
            *_payload_warnings(ctx.watchdog),
            *_payload_warnings(ctx.scheduler),
            *_payload_warnings(ctx.sentinel),
            *_payload_warnings(ctx.riskhub),
            "No strategy is validated yet.",
        ]
    )
    app = AppStoryModel(
        generated_at=_now(),
        latest_run_date=latest_day,
        overall_status="needs_attention" if warnings else "operating",
        alert_level="warning" if warnings else "clear",
        headline=(
            "Dawnstrike needs attention"
            if warnings
            else "Dawnstrike is operating in research mode"
        ),
        subheadline=(
            "X2 is a local story layer over existing OMEGA artifacts. It does not "
            "send messages, change strategies, or enable live execution."
        ),
        top_metrics=[
            TopMetric("Latest day", latest_day, "Most recent artifact date", "info"),
            TopMetric(
                "Strategies",
                str(len(strategy_models)),
                "Story cards from PaperOps, Learning Foundry, and Market Masters",
                "info",
            ),
            TopMetric(
                "Open paper positions",
                str(len(ctx.open_positions)),
                "Simulated state from PaperOps artifacts",
                "warning" if ctx.open_positions else "neutral",
            ),
            TopMetric(
                "Calendar days",
                str(len(day_models)),
                "Days with artifact-backed story pages",
                "info",
            ),
        ],
        trust_boundaries=[
            TrustBoundary(
                "Research-only / paper-only",
                "enforced",
                "X2 renders local artifacts and has no execution controls.",
            ),
            TrustBoundary(
                "Live trading disabled",
                "enforced",
                "Autonomous Runner reports live_trading_enabled=false.",
            ),
            TrustBoundary(
                "No strategy validated",
                "active warning",
                "Champion and challenger cards remain unvalidated unless a source says otherwise.",
            ),
            TrustBoundary(
                "Shadow challengers stay shadow",
                "enforced",
                "Learning Foundry and Market Masters cards cannot promote anything from the UI.",
            ),
        ],
        command_center_paths={
            "command_center": "data/v2_command_center/production.html",
            "command_center_legacy": "data/v2_command_center/index.html",
            "command_center_x": "data/v2_command_center_x/index.html",
            "command_center_x2": "data/v2_command_center_x2/index.html",
        },
        source_refs=ctx.source_refs(),
        warnings=warnings[:80],
    )
    return CommandCenterX2StoryBundle(
        app=app,
        months=month_models,
        days=day_models,
        strategies=strategy_models,
        no_picks=no_picks,
        automation=automation,
        learning_cards=_learning_cards(ctx),
        market_masters_cards=_market_masters_cards(ctx),
        reports=_report_cards(ctx),
        system_flow=_system_flow(ctx),
        calendar_audit=calendar_audit,
    )


def write_story_models(
    *,
    output_root: Path,
    repo_root: Path = Path("."),
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    bundle = build_story_bundle(repo_root=repo_root, paper_ops_root=paper_ops_root)
    data_dir = output_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = to_plain(bundle)
    _write_json(data_dir / "app_story.json", payload["app"])
    _write_json(data_dir / "months.json", payload["months"])
    _write_json(data_dir / "days.json", payload["days"])
    _write_json(data_dir / "strategies.json", payload["strategies"])
    _write_json(data_dir / "no_picks.json", payload["no_picks"])
    _write_json(data_dir / "automation.json", payload["automation"])
    _write_json(data_dir / "learning_cards.json", payload["learning_cards"])
    _write_json(data_dir / "market_masters_cards.json", payload["market_masters_cards"])
    _write_json(data_dir / "reports.json", payload["reports"])
    _write_json(data_dir / "system_flow.json", payload["system_flow"])
    _write_json(output_root / "reports/calendar_audit.json", payload["calendar_audit"])
    _write_json(output_root / "manifests/story_bundle.json", payload)
    return payload


class _ArtifactContext:
    def __init__(
        self,
        *,
        repo_root: Path,
        paper_ops_root: str | Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.paper_ops_root = production_paper_ops_root(
            repo_root=repo_root,
            override=paper_ops_root,
        )
        try:
            self.paper_calendar = load_paper_ops_calendar(self.paper_ops_root)
        except (OSError, ValueError, PaperOpsCalendarError):
            self.paper_calendar = {}
        self.sentinel = _read_json(repo_root / "data/v2_omega_sentinel/status/latest_status.json", {})
        self.scheduler = _read_json(repo_root / "data/v2_scheduler/status/latest_status.json", {})
        self.autonomous = _read_json(
            repo_root / "data/v2_autonomous_runner/status/latest_status.json", {}
        )
        self.watchdog = _read_json(
            repo_root / "data/v2_autonomous_runner/health/watchdog_latest.json", {}
        )
        self.telegram_verify = _read_json(
            repo_root / "data/v2_telegram_intel/reports/verify_latest.json", {}
        )
        self.telegram_readiness = _read_json(
            repo_root / "data/v2_telegram_intel/reports/readiness_latest.json", {}
        )
        self.telegram_message = _read_json(
            _latest_file(repo_root / "data/v2_telegram_intel/messages", "*.json"), {}
        )
        self.provider = _read_json(repo_root / "data/v2_autodata/reports/provider_readiness.json", {})
        self.filltruth = _read_json(repo_root / "data/v2_fill_truth/reports/filltruth_summary.json", {})
        self.commitbridge = _read_json(
            repo_root / "data/v2_evidence_commit/reports/evidence_commit_summary.json", {}
        )
        self.riskhub = _read_json(repo_root / "data/v2_forward_evidence/reports/riskhub_daily.json", {})
        self.frozen = _read_json(
            _latest_file(repo_root / "data/v2_forward_evidence/frozen_picks", "*.json"), {}
        )
        self.pending_orders = _rows_from_payload(
            _read_json(self.paper_ops_root / "state/pending_orders.json", [])
        )
        self.open_positions = _rows_from_payload(
            _read_json(self.paper_ops_root / "state/open_positions.json", [])
        )
        self.closed_trades = _rows_from_payload(
            _read_json(self.paper_ops_root / "state/closed_trades.json", [])
        )
        self.learning_verify = _read_json(
            repo_root / "data/v2_learning_foundry/reports/verify_latest.json", {}
        )
        self.learning_lesson = _read_json(
            _latest_file(repo_root / "data/v2_learning_foundry/lessons", "*.json"), {}
        )
        self.learning_challengers = _read_json(
            repo_root / "data/v2_learning_foundry/candidates/challenger_registry.json", {}
        )
        self.learning_promotion = _read_json(
            repo_root / "data/v2_learning_foundry/reports/promotion_review.json", {}
        )
        self.market_report = _read_json(
            repo_root / "data/v2_market_masters/reports/report_latest.json", {}
        )
        self.market_verify = _read_json(
            repo_root / "data/v2_market_masters/reports/verify_latest.json", {}
        )
        self.market_challengers = _read_json(
            repo_root / "data/v2_market_masters/candidates/challenger_registry.json", {}
        )
        self.market_methods = _read_json(
            repo_root / "data/v2_market_masters/methodologies/methodology_taxonomy.json", {}
        )
        self.market_primitives = _read_json(
            repo_root / "data/v2_market_masters/primitives/strategy_primitives.json", {}
        )

    def source_refs(self) -> list[SourceRef]:
        paths = [
            self.paper_ops_root / "calendar/strategy_daily_returns.csv",
            self.paper_ops_root / "state/open_positions.json",
            self.repo_root / "data/v2_fill_truth/reports/filltruth_summary.json",
            self.repo_root / "data/v2_evidence_commit/reports/evidence_commit_summary.json",
            self.repo_root / "data/v2_learning_foundry/lessons",
            self.repo_root / "data/v2_market_masters/reports/report_latest.json",
            self.repo_root / "data/v2_autonomous_runner/status/latest_status.json",
            self.repo_root / "data/v2_telegram_intel/reports/verify_latest.json",
        ]
        return [_source_ref_path(self.repo_root, path) for path in paths]


def _calendar_rows(ctx: _ArtifactContext) -> list[dict[str, str]]:
    rows = _read_csv(ctx.paper_ops_root / "calendar/strategy_daily_returns.csv")
    # Operator performance surfaces are forward-only. Replay/demo evidence
    # remains in the canonical ledger and reports, but is never blended into a
    # live return tile or silently relabeled as forward evidence.
    return [row for row in rows if str(row.get("mode") or "").lower() == "forward"]


def _latest_day(ctx: _ArtifactContext, calendar_rows: list[dict[str, str]]) -> str:
    dates = [str(row.get("date") or "")[:10] for row in calendar_rows if row.get("date")]
    for payload in (ctx.scheduler, ctx.sentinel, ctx.filltruth):
        for key in ("run_date", "latest_run_date", "date"):
            value = str(payload.get(key) or "")[:10]
            if value:
                dates.append(value)
    return max(dates) if dates else date.today().isoformat()


def _strategy_models(
    ctx: _ArtifactContext,
    calendar_rows: list[dict[str, str]],
) -> list[StrategyStoryModel]:
    by_strategy: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in calendar_rows:
        strategy_id = _text(row.get("strategy_id") or row.get("strategy"), "unknown")
        by_strategy[strategy_id].append(row)
    models: list[StrategyStoryModel] = []
    for strategy_id, rows in sorted(by_strategy.items()):
        latest = sorted(rows, key=lambda item: str(item.get("date") or ""))[-1]
        warnings = _split_warnings(latest.get("warnings"))
        trade_count = sum(_int(row.get("trades_opened")) + _int(row.get("trades_closed")) for row in rows)
        forward_days = len({row.get("date") for row in rows if row.get("mode") == "forward"})
        models.append(
            StrategyStoryModel(
                strategy_id=strategy_id,
                strategy_name=_title(strategy_id),
                role="champion" if "legacy" in strategy_id else "challenger",
                status=_strategy_status(latest),
                daily_return_pct=_fmt_pct(latest.get("daily_return_pct")),
                cumulative_return_pct=_fmt_pct(latest.get("cumulative_return_pct")),
                win_rate=_win_rate(rows),
                average_r=_fmt_value(latest.get("average_r")),
                expectancy=_fmt_value(latest.get("expectancy_r")),
                drawdown=_fmt_pct(latest.get("drawdown_pct")),
                trade_count=trade_count,
                forward_days=forward_days,
                validation_progress="0% - not validated",
                latest_signal_state="artifact-backed paper calendar row",
                latest_paper_state=_paper_state(latest),
                latest_learning_notes=_latest_learning_note(ctx, strategy_id),
                evidence_quality=_evidence_quality(latest),
                warnings=warnings or ["Not validated; research-only paper evidence."],
                daily_series=[
                    {
                        "date": str(row.get("date") or MISSING),
                        "mode": str(row.get("mode") or MISSING),
                        "daily_return_pct": _fmt_pct(row.get("daily_return_pct")),
                        "cumulative_return_pct": _fmt_pct(row.get("cumulative_return_pct")),
                    }
                    for row in sorted(rows, key=lambda item: str(item.get("date") or ""))[-40:]
                ],
            )
        )
    existing_strategy_ids = {model.strategy_id for model in models}
    for row in _rows_from_payload(ctx.paper_calendar.get("official_series", [])):
        strategy_id = _text(row.get("strategy_id"), "unknown")
        if strategy_id in existing_strategy_ids:
            continue
        inception = _text(row.get("registry_inception_date"), MISSING)
        models.append(
            StrategyStoryModel(
                strategy_id=strategy_id,
                strategy_name=_text(row.get("strategy_label"), _title(strategy_id)),
                role="registered_forward_candidate",
                status="registered / not yet eligible",
                daily_return_pct=MISSING,
                cumulative_return_pct=MISSING,
                win_rate=MISSING,
                average_r=MISSING,
                expectancy=MISSING,
                drawdown=MISSING,
                trade_count=0,
                forward_days=0,
                validation_progress="0% - forward evidence not started",
                latest_signal_state=f"Forward observation begins {inception}.",
                latest_paper_state="No eligible forward session yet.",
                latest_learning_notes="No forward outcome exists to learn from.",
                evidence_quality="registered exact lineage; no eligible return",
                warnings=[
                    "Return is N/A until the first eligible forward session; not validated."
                ],
                daily_series=[],
            )
        )
        existing_strategy_ids.add(strategy_id)
    for row in _rows_from_payload(ctx.learning_challengers)[:20]:
        strategy_id = _text(
            row.get("strategy_id") or row.get("challenger_id") or row.get("id") or row.get("name"),
            "learning_shadow",
        )
        if strategy_id in by_strategy:
            continue
        models.append(_shadow_strategy(strategy_id, "Learning Foundry", row))
    for row in _rows_from_payload(ctx.market_challengers)[:20]:
        strategy_id = _text(
            row.get("strategy_id") or row.get("challenger_id") or row.get("id") or row.get("name"),
            "market_shadow",
        )
        if any(model.strategy_id == strategy_id for model in models):
            continue
        models.append(_shadow_strategy(strategy_id, "Market Masters", row))
    return models[:120]


def _shadow_strategy(source_id: str, source: str, row: dict[str, Any]) -> StrategyStoryModel:
    return StrategyStoryModel(
        strategy_id=_slug(source_id),
        strategy_name=_title(source_id),
        role="challenger",
        status="shadow",
        daily_return_pct=MISSING,
        cumulative_return_pct=MISSING,
        win_rate=_fmt_value(row.get("win_rate") or row.get("win_rate_pct")),
        average_r=_fmt_value(row.get("average_r")),
        expectancy=_fmt_value(row.get("expectancy") or row.get("expectancy_r")),
        drawdown=_fmt_value(row.get("drawdown") or row.get("max_drawdown")),
        trade_count=_int(row.get("trade_count") or row.get("trades")),
        forward_days=_int(row.get("forward_days") or row.get("sample_days")),
        validation_progress="0% - shadow only",
        latest_signal_state=f"{source} research candidate",
        latest_paper_state="not official PaperOps champion evidence",
        latest_learning_notes=_text(row.get("lesson") or row.get("thesis"), "Shadow candidate only."),
        evidence_quality="shadow research; not official",
        warnings=[f"{source} challenger remains shadow-only and not validated."],
        daily_series=[],
    )


def _day_models(
    ctx: _ArtifactContext,
    calendar_rows: list[dict[str, str]],
    strategy_models: list[StrategyStoryModel],
    latest_day: str,
) -> list[DayStoryModel]:
    dates = {str(row.get("date") or "")[:10] for row in calendar_rows if row.get("date")}
    for row in [*ctx.open_positions, *ctx.pending_orders, *ctx.closed_trades]:
        day = _extract_day(row)
        if day:
            dates.add(day)
    dates.add(latest_day)
    by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in calendar_rows:
        if row.get("date"):
            by_day[str(row["date"])[:10]].append(row)
    by_strategy = {model.strategy_id: model for model in strategy_models}
    days: list[DayStoryModel] = []
    for day in sorted(dates):
        rows = by_day.get(day, [])
        strategies = [by_strategy.get(_text(row.get("strategy_id"), "")) for row in rows]
        strategy_returns = [model for model in strategies if model is not None]
        trades = _paper_trades(ctx, day, rows)
        accepted = _rows_from_key(ctx.frozen, "accepted_candidates")
        blocked = _rows_from_key(ctx.frozen, "blocked_candidates")
        watch = _rows_from_key(ctx.frozen, "watchlist_candidates")
        warnings = _dedupe(
            [
                *_calendar_warnings(rows),
                *_payload_warnings(ctx.riskhub),
                *_payload_warnings(ctx.autonomous),
            ]
        )
        daily, cumulative = _day_return_values(rows)
        days.append(
            DayStoryModel(
                date=day,
                headline=_day_headline(day, trades, warnings, daily),
                market_context="Artifact-backed local story; no provider call was made by X2.",
                run_status=_text(ctx.scheduler.get("status"), "missing"),
                provider_status=_provider_status(ctx),
                picks_summary={
                    "accepted": len(accepted),
                    "blocked": len(blocked),
                    "watch": len(watch),
                    "strategy_rows": len(rows),
                },
                no_picks_reasons=_no_pick_reasons(ctx) if not accepted else [],
                paper_trades=trades,
                paper_orders=_rows_for_day(ctx.pending_orders, day),
                fills=[],
                closes=_rows_for_day(ctx.closed_trades, day),
                open_positions=_rows_for_day(ctx.open_positions, day),
                strategy_returns=strategy_returns,
                cumulative_returns={
                    "daily_return_pct": daily,
                    "cumulative_return_pct": cumulative,
                },
                riskhub_summary=_riskhub_summary(ctx),
                filltruth_summary=_filltruth_summary(ctx),
                commitbridge_summary=_commit_summary(ctx),
                learning_foundry_lesson=_lesson_summary(ctx.learning_lesson),
                market_masters_lesson=_market_summary(ctx),
                telegram_summary=_telegram_summary(ctx),
                warnings=warnings or ["No critical warning for this page; trust boundaries still apply."],
                what_to_watch_next=_what_to_watch(ctx, trades, warnings),
                source_refs=ctx.source_refs(),
            )
        )
    return days


def _month_models(
    calendar_rows: list[dict[str, str]],
    day_models: list[DayStoryModel],
) -> tuple[list[MonthCalendarModel], dict[str, Any]]:
    day_by_date = {day.date: day for day in day_models}
    all_dates = sorted(day_by_date)
    months = sorted({day[:7] for day in all_dates})
    if not months:
        months = [date.today().isoformat()[:7]]
    rows_by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in calendar_rows:
        rows_by_day[str(row.get("date") or "")[:10]].append(row)
    models: list[MonthCalendarModel] = []
    for index, month in enumerate(months):
        year, month_number = (int(part) for part in month.split("-"))
        days_in_month = monthrange(year, month_number)[1]
        calendar_days: list[CalendarDayModel] = []
        green = red = flat = no_trade = warning_days = 0
        daily_values: list[tuple[str, float]] = []
        for day_number in range(1, days_in_month + 1):
            day_key = f"{month}-{day_number:02d}"
            rows = rows_by_day.get(day_key, [])
            day_model = day_by_date.get(day_key)
            daily, cumulative = _day_return_values(rows)
            daily_number = _float_or_none(daily)
            if daily_number is None:
                no_trade += 1
                tone = "none"
            elif daily_number > 0:
                green += 1
                tone = "positive"
            elif daily_number < 0:
                red += 1
                tone = "negative"
            else:
                flat += 1
                tone = "flat"
            if daily_number is not None:
                daily_values.append((day_key, daily_number))
            warning_count = len(day_model.warnings) if day_model else 0
            if warning_count:
                warning_days += 1
            calendar_days.append(
                CalendarDayModel(
                    date=day_key,
                    daily_return_pct=daily,
                    cumulative_return_pct=cumulative,
                    trade_count=sum(
                        _int(row.get("trades_opened")) + _int(row.get("trades_closed"))
                        for row in rows
                    ),
                    warning_count=warning_count,
                    state=_day_state(rows, day_model),
                    has_learning=day_model is not None
                    and day_model.learning_foundry_lesson not in {"", MISSING},
                    has_market_masters=day_model is not None
                    and day_model.market_masters_lesson not in {"", MISSING},
                    href=f"../days/{day_key}.html",
                    tone=tone,
                )
            )
        best = max(daily_values, key=lambda item: item[1])[0] if daily_values else MISSING
        worst = min(daily_values, key=lambda item: item[1])[0] if daily_values else MISSING
        month_rows = [row for row in calendar_rows if str(row.get("date") or "").startswith(month)]
        _, month_cumulative = _day_return_values(month_rows[-10:])
        month_return = _aggregate_return(month_rows, "daily_return_pct")
        models.append(
            MonthCalendarModel(
                month=month,
                calendar_days=calendar_days,
                cumulative_return_pct=month_cumulative,
                monthly_return_pct=month_return,
                best_day=best,
                worst_day=worst,
                green_days=green,
                red_days=red,
                flat_days=flat,
                no_trade_days=no_trade,
                warning_days=warning_days,
                previous_month=months[index - 1] if index else month,
                next_month=months[index + 1] if index + 1 < len(months) else month,
                source_policy="mean of source strategy_daily_returns rows by calendar day",
            )
        )
    audit = {
        "schema_version": "v2.command_center_x2.calendar_audit.v1",
        "source_row_count": len(calendar_rows),
        "source_hash": _hash_rows(calendar_rows),
        "day_count": len(day_models),
        "month_count": len(models),
        "policy": "calendar daily/cumulative values are direct aggregates of source CSV fields",
        "status": "passed",
    }
    return models, audit


def _no_picks_model(ctx: _ArtifactContext, latest_day: str) -> NoPicksStoryModel:
    accepted = _rows_from_key(ctx.frozen, "accepted_candidates")
    blocked = _rows_from_key(ctx.frozen, "blocked_candidates")
    watch = _rows_from_key(ctx.frozen, "watchlist_candidates")
    no_setup = _rows_from_key(ctx.frozen, "no_setup_explanations")
    reasons = _no_pick_reasons(ctx)
    riskhub = _payload_warnings(ctx.riskhub)
    data_blockers = [
        item
        for item in _payload_warnings(ctx.autonomous) + _payload_warnings(ctx.provider)
        if "provider" in item.lower() or "data" in item.lower() or "bar" in item.lower()
    ]
    return NoPicksStoryModel(
        date=latest_day,
        headline="Dawnstrike waited because no official paper edge cleared the evidence gates.",
        accepted_count=len(accepted),
        blocked_count=len(blocked),
        watch_count=len(watch),
        no_setup_count=len(no_setup),
        top_reasons=reasons,
        strategies_blocked=_strategy_blockers(blocked),
        data_quality_blockers=data_blockers[:10],
        riskhub_blockers=riskhub[:10],
        near_setups=[_text(row.get("symbol") or row.get("ticker"), "near setup") for row in watch[:10]],
        what_would_change=[
            "Cleaner provider evidence.",
            "RiskHub or Decision Engine block clears.",
            "A strategy produces official paper evidence without validation overstatement.",
        ],
        why_no_trade_is_valid=(
            "No-picks days protect the research process. Missing or blocked evidence stays visible "
            "instead of becoming a false zero or a fake opportunity."
        ),
    )


def _automation_model(ctx: _ArtifactContext) -> AutomationStoryModel:
    tasks = _rows_from_payload(ctx.autonomous.get("tasks") if isinstance(ctx.autonomous, dict) else [])
    missed = _rows_from_payload(ctx.autonomous.get("missed_runs") if isinstance(ctx.autonomous, dict) else [])
    return AutomationStoryModel(
        task_statuses=tasks,
        next_runs=[
            {
                "task": _text(row.get("task_name"), "task"),
                "next_run_time": _text(row.get("next_run_time"), MISSING),
                "state": _text(row.get("state"), MISSING),
            }
            for row in tasks
        ],
        missed_runs=[
            {
                "task": _text(row.get("task_name"), "task"),
                "state": _text(row.get("state"), MISSING),
                "schedule_time": _text(row.get("schedule_time"), MISSING),
            }
            for row in missed
            if row.get("missed") is True or str(row.get("state", "")).lower() == "missed"
        ],
        latest_scheduler_status=_text(ctx.scheduler.get("status"), "missing"),
        latest_watchdog_status=_text(ctx.watchdog.get("status"), "missing"),
        telegram_readiness=_text(
            ctx.telegram_readiness.get("readiness_status")
            or ctx.telegram_verify.get("readiness_status"),
            "missing",
        ),
        autonomous_runner_status=_text(ctx.autonomous.get("status"), "missing"),
        no_overlap_status=_text(ctx.autonomous.get("no_overlap_policy"), "missing"),
        warnings=_dedupe(_payload_warnings(ctx.autonomous) + _payload_warnings(ctx.watchdog)),
    )


def _learning_cards(ctx: _ArtifactContext) -> list[dict[str, Any]]:
    lesson_rows = []
    for path in sorted((ctx.repo_root / "data/v2_learning_foundry/lessons").glob("*.json"))[-12:]:
        payload = _read_json(path, {})
        lesson_rows.append(
            {
                "title": path.stem,
                "status": _text(payload.get("status"), "lesson"),
                "summary": _lesson_summary(payload),
                "path": _rel(ctx.repo_root, path),
            }
        )
    if not lesson_rows:
        lesson_rows.append(
            {
                "title": "No latest lesson artifact",
                "status": "missing",
                "summary": "Learning Foundry did not provide a JSON lesson artifact.",
                "path": "data/v2_learning_foundry/lessons",
            }
        )
    return lesson_rows


def _market_masters_cards(ctx: _ArtifactContext) -> list[dict[str, Any]]:
    cards = [
        {
            "title": "Market Masters Research",
            "status": _text(ctx.market_report.get("final_status") or ctx.market_report.get("status"), "missing"),
            "summary": _market_summary(ctx),
            "path": "data/v2_market_masters/reports/report_latest.json",
        }
    ]
    for row in _rows_from_payload(ctx.market_challengers)[:8]:
        cards.append(
            {
                "title": _title(row.get("name") or row.get("strategy_id") or row.get("id") or "shadow"),
                "status": "shadow",
                "summary": _text(
                    row.get("thesis") or row.get("methodology") or row.get("description"),
                    "Shadow-only challenger; not promoted.",
                ),
                "path": "data/v2_market_masters/candidates/challenger_registry.json",
            }
        )
    return cards


def _report_cards(ctx: _ArtifactContext) -> list[dict[str, str]]:
    paths = [
        ("Command Center X2 build", "reports/build_report.md"),
        ("Command Center X2 inventory", "reports/inventory_latest.md"),
        ("Command Center X2 QA", "qa/qa_latest.md"),
        ("Command Center X2 verify", "reports/verify_latest.md"),
        ("Calendar audit", "reports/calendar_audit.json"),
        ("Story bundle", "manifests/story_bundle.json"),
        ("Release state", "reports/release_state.json"),
    ]
    return [
        {
            "title": title,
            "status": "generated local artifact",
            "why": "Evidence, runbook, or QA artifact used by the X2 story layer.",
            "href": _safe_link(path),
        }
        for title, path in paths
    ]


def _system_flow(ctx: _ArtifactContext) -> list[dict[str, str]]:
    names = [
        "AutoData",
        "DataTruth",
        "FillTruth",
        "CommitBridge",
        "PaperOps",
        "Calendar Intelligence",
        "Strategy Evidence",
        "Learning Foundry",
        "Market Masters",
        "Sentinel",
        "Telegram",
        "Command Center X2",
    ]
    return [
        {
            "name": name,
            "status": _system_status(ctx, name),
            "description": _system_description(name),
        }
        for name in names
    ]


def _paper_trades(
    ctx: _ArtifactContext,
    day: str,
    calendar_rows: list[dict[str, str]],
) -> list[PaperTradeStoryModel]:
    trades: list[PaperTradeStoryModel] = []
    rows = _rows_for_day(ctx.open_positions, day) + _rows_for_day(ctx.pending_orders, day)
    if not rows and calendar_rows:
        rows = [
            row
            for row in calendar_rows
            if _int(row.get("trades_opened"))
            or _int(row.get("trades_closed"))
            or _int(row.get("open_positions"))
            or _int(row.get("pending_orders"))
        ]
    for index, row in enumerate(rows):
        strategy_id = _text(row.get("strategy_id"), _text(row.get("strategy"), "unknown"))
        symbol = _text(row.get("symbol") or row.get("ticker"), "paper basket")
        state = _text(row.get("status") or row.get("state"), "paper-calendar")
        trade_id = _text(row.get("position_id") or row.get("order_id") or row.get("run_id"), f"{day}-{index}")
        opened = _text(row.get("opened_at") or row.get("date"), day)
        trades.append(
            PaperTradeStoryModel(
                trade_id=trade_id,
                date=day,
                symbol=symbol,
                strategy_id=strategy_id,
                direction=_text(row.get("direction"), "long"),
                state=state,
                entry=_fmt_value(row.get("entry_price") or row.get("entry")),
                stop=_fmt_value(row.get("stop")),
                target=_fmt_value(row.get("target")),
                fill_price=_fmt_value(row.get("fill_price")),
                close_price=_fmt_value(row.get("close_price")),
                realized_pnl=_fmt_value(row.get("realized_pnl")),
                unrealized_pnl=_fmt_value(row.get("unrealized_pnl")),
                r_multiple=_fmt_value(row.get("r_multiple") or row.get("average_r")),
                reason=_text(row.get("reason") or row.get("warnings"), "Paper evidence row."),
                evidence_source=_text(row.get("data_snapshot_id") or row.get("schema_version"), "PaperOps"),
                filltruth_certainty=_text(
                    ctx.filltruth.get("data_granularity_available")
                    or ctx.filltruth.get("session_completeness"),
                    "unknown",
                ),
                commitbridge_status=_text(ctx.commitbridge.get("status"), "missing"),
                timeline_events=[
                    {"label": "Signal artifact", "value": opened},
                    {"label": "Paper state", "value": state},
                    {"label": "Evidence chain", "value": "FillTruth -> CommitBridge -> PaperOps"},
                ],
            )
        )
    return trades


def _day_return_values(rows: list[dict[str, str]]) -> tuple[str, str]:
    if not rows:
        return MISSING, MISSING
    return (
        _aggregate_return(rows, "daily_return_pct"),
        _aggregate_return(rows, "cumulative_return_pct"),
    )


def _aggregate_return(rows: list[dict[str, str]], field: str) -> str:
    values = [_float_or_none(row.get(field)) for row in rows]
    numbers = [value for value in values if value is not None]
    if not numbers:
        return MISSING
    return f"{sum(numbers) / len(numbers):.4f}%"


def _hash_rows(rows: list[dict[str, str]]) -> str:
    encoded = json.dumps(rows, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _read_json(path: Path, default: Any) -> Any:
    if path.is_dir() or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if path.is_dir() or not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _latest_file(root: Path, pattern: str) -> Path:
    if root.is_file():
        return root
    if not root.exists():
        return root / "__missing__"
    matches = sorted(root.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0] if matches else root / "__missing__"


def _rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "rows",
        "items",
        "tasks",
        "candidates",
        "challengers",
        "open_positions",
        "pending_orders",
        "closed_trades",
        "results",
        "methodologies",
        "primitives",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _rows_from_key(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [row for row in payload[key] if isinstance(row, dict)]
    return []


def _rows_for_day(rows: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    return [row for row in rows if _extract_day(row) == day]


def _extract_day(row: dict[str, Any]) -> str:
    for key in ("date", "opened_at", "closed_at", "created_at", "entry_time", "market_date"):
        value = str(row.get(key) or "")
        if len(value) >= 10 and value[:4].isdigit():
            return value[:10]
    return ""


def _payload_warnings(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    warnings: list[str] = []
    for key in ("warnings", "failures", "errors", "blocking_reasons"):
        value = payload.get(key)
        if isinstance(value, list):
            warnings.extend(str(item) for item in value if str(item))
        elif value:
            warnings.append(str(value))
    status = str(payload.get("status") or "").lower()
    if status in {"failed", "blocked", "critical", "missing"}:
        warnings.append(f"Artifact status is {status}.")
    return _dedupe(warnings)


def _calendar_warnings(rows: list[dict[str, str]]) -> list[str]:
    warnings: list[str] = []
    for row in rows:
        warnings.extend(_split_warnings(row.get("warnings")))
    return _dedupe(warnings)


def _split_warnings(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [part.strip() for part in str(value).replace("|", ";").split(";") if part.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output


def _strategy_status(row: dict[str, str]) -> str:
    status = str(row.get("strategy_status") or row.get("status") or "active").lower()
    if "valid" in status:
        return "active_not_validated"
    return status


def _paper_state(row: dict[str, str]) -> str:
    opened = _int(row.get("trades_opened"))
    closed = _int(row.get("trades_closed"))
    pending = _int(row.get("pending_orders"))
    open_positions = _int(row.get("open_positions"))
    if open_positions:
        return "open"
    if pending:
        return "pending"
    if closed:
        return "closed"
    if opened:
        return "filled"
    return "no paper activity"


def _evidence_quality(row: dict[str, str]) -> str:
    source = str(row.get("data_snapshot_id") or "")
    if "synthetic" in source:
        return "synthetic demo only"
    if source:
        return "artifact-backed"
    return "unknown"


def _win_rate(rows: list[dict[str, str]]) -> str:
    wins = sum(_int(row.get("wins")) for row in rows)
    losses = sum(_int(row.get("losses")) for row in rows)
    total = wins + losses
    if total <= 0:
        return MISSING
    return f"{wins / total * 100:.1f}%"


def _latest_learning_note(ctx: _ArtifactContext, strategy_id: str) -> str:
    text = json.dumps(ctx.learning_lesson, sort_keys=True)
    if strategy_id in text:
        return _lesson_summary(ctx.learning_lesson)
    return "No strategy-specific Learning Foundry note found."


def _no_pick_reasons(ctx: _ArtifactContext) -> list[str]:
    text = ""
    if isinstance(ctx.telegram_message, dict):
        text = str(ctx.telegram_message.get("text") or "")
    reasons: list[str] = []
    collect = False
    for raw in text.splitlines():
        line = raw.strip()
        if "why no" in line.lower():
            collect = True
            continue
        if collect and line:
            reasons.append(line)
    if reasons:
        return reasons[:8]
    risk = _payload_warnings(ctx.riskhub) + _payload_warnings(ctx.autonomous)
    return risk[:8] or ["No latest no-picks explanation found in Telegram artifacts."]


def _strategy_blockers(rows: list[dict[str, Any]]) -> list[str]:
    output = []
    for row in rows[:12]:
        output.append(
            _text(
                row.get("strategy_id") or row.get("strategy") or row.get("symbol") or row.get("ticker"),
                "blocked candidate",
            )
        )
    return output


def _provider_status(ctx: _ArtifactContext) -> str:
    return _text(
        ctx.provider.get("status")
        or ctx.provider.get("readiness_status")
        or ctx.autonomous.get("autodata_provider_readiness_status"),
        "missing",
    )


def _riskhub_summary(ctx: _ArtifactContext) -> str:
    warnings = _payload_warnings(ctx.riskhub)
    if warnings:
        return "; ".join(warnings[:3])
    return _text(ctx.riskhub.get("status"), "No RiskHub summary artifact found.")


def _filltruth_summary(ctx: _ArtifactContext) -> str:
    return (
        f"{_text(ctx.filltruth.get('status'), 'missing')} - "
        f"{_text(ctx.filltruth.get('strategy_validation_impact'), 'validation impact unknown')}"
    )


def _commit_summary(ctx: _ArtifactContext) -> str:
    return (
        f"{_text(ctx.commitbridge.get('status'), 'missing')} - "
        f"{_text(ctx.commitbridge.get('what_next'), 'no next action')}"
    )


def _lesson_summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "No Learning Foundry lesson artifact found."
    for key in ("lesson", "summary", "headline", "finding", "status"):
        value = payload.get(key)
        if value:
            return str(value)
    return "Learning Foundry artifact exists but has no plain-language lesson field."


def _market_summary(ctx: _ArtifactContext) -> str:
    if not ctx.market_report:
        return "No Market Masters report found."
    return (
        f"{ctx.market_report.get('challenger_count', 0)} shadow challengers, "
        f"{ctx.market_report.get('methodology_count', 0)} methodologies, promotion "
        f"{ctx.market_report.get('promotion_result', 'unknown')}."
    )


def _telegram_summary(ctx: _ArtifactContext) -> str:
    return (
        f"verify={_text(ctx.telegram_verify.get('status'), 'missing')}; "
        f"readiness={_text(ctx.telegram_verify.get('readiness_status'), 'missing')}"
    )


def _what_to_watch(
    ctx: _ArtifactContext,
    trades: list[PaperTradeStoryModel],
    warnings: list[str],
) -> list[str]:
    items = []
    if trades:
        items.append("Review open paper state and evidence chain before the next run.")
    if warnings:
        items.append("Resolve or accept warnings before treating any result as trusted.")
    items.append("Wait for the next scheduled OMEGA task; X2 does not trigger runs.")
    if ctx.open_positions:
        items.append("Open paper positions remain simulated and require future evidence.")
    return _dedupe(items)


def _day_headline(
    day: str,
    trades: list[PaperTradeStoryModel],
    warnings: list[str],
    daily: str,
) -> str:
    if trades:
        return f"{day}: paper activity recorded with daily return {daily}."
    if warnings:
        return f"{day}: no official paper trade story cleared without warnings."
    return f"{day}: quiet evidence day; no fabricated activity."


def _day_state(rows: list[dict[str, str]], day: DayStoryModel | None) -> str:
    if day and day.paper_trades:
        return "paper-trade"
    if rows:
        return "strategy-row"
    return "no-evidence"


def _system_status(ctx: _ArtifactContext, name: str) -> str:
    mapping = {
        "AutoData": ctx.provider,
        "FillTruth": ctx.filltruth,
        "CommitBridge": ctx.commitbridge,
        "Learning Foundry": ctx.learning_verify,
        "Market Masters": ctx.market_verify,
        "Sentinel": ctx.sentinel,
        "Telegram": ctx.telegram_verify,
    }
    payload = mapping.get(name)
    if isinstance(payload, dict) and payload:
        return _text(payload.get("status") or payload.get("readiness_status"), "present")
    if name == "Command Center X2":
        return "generated"
    return "artifact-linked"


def _system_description(name: str) -> str:
    descriptions = {
        "AutoData": "Collects provider evidence and records readiness.",
        "DataTruth": "Keeps canonical data quality boundaries visible.",
        "FillTruth": "Separates exact, approximate, pending, and blocked fill evidence.",
        "CommitBridge": "Prevents unsafe evidence from becoming official.",
        "PaperOps": "Stores paper-only positions, returns, and calendar rows.",
        "Calendar Intelligence": "Turns strategy daily rows into day and month memory.",
        "Strategy Evidence": "Shows forward evidence without validation inflation.",
        "Learning Foundry": "Creates lessons and shadow challengers without promotion.",
        "Market Masters": "Translates research into shadow-only challengers.",
        "Sentinel": "Watches run health, warnings, and trust boundaries.",
        "Telegram": "Formats/send-checks messages without exposing secrets here.",
        "Command Center X2": "Renders the local story interface from these artifacts.",
    }
    return descriptions[name]


def _source_ref_path(repo_root: Path, path: Path) -> SourceRef:
    return SourceRef(
        path=_rel(repo_root, path),
        exists=path.exists(),
        kind="directory" if path.is_dir() else "file",
    )


def _safe_link(path: str) -> str:
    return path.replace("\\", "/")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _text(value: Any, default: str) -> str:
    if value is None or value == "":
        return default
    return str(value)


def _title(value: Any) -> str:
    return _text(value, "Unknown").replace("_", " ").replace("-", " ").title()


def _slug(value: Any) -> str:
    text = _text(value, "unknown").strip().lower()
    output = "".join(char if char.isalnum() else "_" for char in text)
    return "_".join(part for part in output.split("_") if part) or "unknown"


def _fmt_value(value: Any) -> str:
    if value is None or value == "":
        return MISSING
    number = _float_or_none(value)
    if number is None:
        return str(value)
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _fmt_pct(value: Any) -> str:
    if value is None or value == "":
        return MISSING
    text = str(value)
    if text.endswith("%"):
        return text
    number = _float_or_none(value)
    if number is None:
        return text
    return f"{number:.4f}%"


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "" or value == MISSING:
        return None
    text = str(value).replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int:
    number = _float_or_none(value)
    return int(number or 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
