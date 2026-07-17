"""Read-only artifact adapters for Dawnstrike Interface Apex."""

# ruff: noqa: E501

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from intraday_scanner.paper_ops_root import production_paper_ops_root
from intraday_scanner.v2.interface_apex.models import (
    CalendarDayTile,
    CalendarModel,
    CalendarMonth,
    DayModel,
    IntelligenceModel,
    InterfaceApexModel,
    MissionModel,
    NoPicksModel,
    StrategyModel,
    SystemModel,
    TradeModel,
)


def build_apex_model_from_artifacts(
    *,
    repo_root: Path = Path("."),
    paper_ops_root: str | Path | None = None,
) -> InterfaceApexModel:
    """Build Apex view models from existing local artifacts only."""

    repo_root = repo_root.resolve()
    x3_data = repo_root / "data/v2_command_center_x3/data"
    day_trade_root = repo_root / "data/v2_day_trade_lab"
    learning_root = repo_root / "data/v2_learning_foundry"
    market_root = repo_root / "data/v2_market_masters"
    runner_root = repo_root / "data/v2_autonomous_runner"
    telegram_root = repo_root / "data/v2_telegram_intel"
    paper_root = production_paper_ops_root(
        repo_root=repo_root,
        override=paper_ops_root,
    )
    datatruth_root = repo_root / "data/v2_data_truth"
    filltruth_root = repo_root / "data/v2_fill_truth"
    commit_root = repo_root / "data/v2_evidence_commit"
    autodata_root = repo_root / "data/v2_autodata"

    app = _read_json(x3_data / "app_story.json", {})
    months_raw = _read_json(x3_data / "months.json", [])
    days_raw = _read_json(x3_data / "days.json", [])
    x3_strategies = _read_json(x3_data / "strategies.json", [])
    no_picks_raw = _read_json(x3_data / "no_picks.json", {})
    x3_system = _read_json(x3_data / "system.json", {})
    day_trade_summary = _read_json(day_trade_root / "reports/corpus_day_trade_summary.json", {})
    day_trade_comparison = _read_json(day_trade_root / "reports/corpus_strategy_comparison.json", [])
    day_trade_qa = _read_json(day_trade_root / "qa/qa_latest.json", {})
    fragility = _read_json(day_trade_root / "robustness/reports/fragility_report.json", {})
    oos = _read_json(day_trade_root / "robustness/out_of_sample/oos_summary.json", {})
    slippage = _read_json(day_trade_root / "robustness/slippage_stress/slippage_stress_summary.json", {})
    learning_verify = _read_json(learning_root / "reports/verify_latest.json", {})
    latest_lesson = _latest_json(learning_root / "lessons", "????-??-??.json")
    latest_regime = _latest_json(learning_root / "regimes", "*_regimes.json")
    market_report = _read_json(market_root / "reports/report_latest.json", {})
    market_challengers = _read_json(market_root / "candidates/challenger_registry.json", {})
    market_methodologies = _read_json(market_root / "methodologies/methodology_taxonomy.json", {})
    market_primitives = _read_json(market_root / "primitives/strategy_primitives.json", {})
    runner_status = _read_json(runner_root / "status/latest_status.json", {})
    watchdog = _read_json(runner_root / "health/watchdog_latest.json", {})
    telegram_verify = _read_json(telegram_root / "reports/verify_latest.json", {})
    telegram_readiness = _read_json(telegram_root / "status/latest_readiness.json", {})
    open_positions = _read_json(paper_root / "state/open_positions.json", [])
    datatruth_latest = _read_json(datatruth_root / "manifests/latest.json", {})
    filltruth_manifest = _read_json(filltruth_root / "manifests/fill_truth_manifest.json", {})
    commit_manifest = _read_json(commit_root / "manifests/evidence_commit_manifest.json", {})

    source_refs = _source_refs(
        repo_root,
        [
            x3_data / "app_story.json",
            x3_data / "months.json",
            x3_data / "days.json",
            x3_data / "strategies.json",
            x3_data / "no_picks.json",
            x3_data / "system.json",
            day_trade_root / "reports/corpus_day_trade_summary.json",
            day_trade_root / "reports/corpus_strategy_comparison.json",
            day_trade_root / "robustness/reports/robustness_report.json",
            learning_root / "reports/verify_latest.json",
            market_root / "reports/report_latest.json",
            runner_root / "status/latest_status.json",
            telegram_root / "reports/verify_latest.json",
            paper_root / "ledger/paper_ledger.jsonl",
            datatruth_root / "manifests/latest.json",
            filltruth_root / "manifests/fill_truth_manifest.json",
            commit_root / "manifests/evidence_commit_manifest.json",
            autodata_root,
        ],
    )
    warnings = _artifact_warnings(source_refs)
    warnings.extend(_string_list(app.get("warnings"))[:12])
    warnings.extend(_string_list(day_trade_summary.get("provider_limitations"))[:4])
    warnings.extend(_string_list(runner_status.get("warnings"))[:8])

    day_trade_examples = _load_day_trade_examples(day_trade_root)
    day_trade_strategy_examples = _group_trades_by_strategy(day_trade_examples)
    calendar = _calendar_model(months_raw)
    days = _day_models(days_raw, day_trade_examples)
    days = _ensure_calendar_day_models(days=days, calendar=calendar)
    trades = _trade_models_from_days(days)
    strategies = _strategy_models(
        day_trade_comparison=day_trade_comparison,
        x3_strategies=x3_strategies,
        market_challengers=market_challengers,
        fragility=fragility,
        oos=oos,
        slippage=slippage,
        examples_by_strategy=day_trade_strategy_examples,
    )
    intelligence = _intelligence_model(
        learning_verify=learning_verify,
        latest_lesson=latest_lesson,
        latest_regime=latest_regime,
        market_report=market_report,
        market_challengers=market_challengers,
        market_methodologies=market_methodologies,
        market_primitives=market_primitives,
    )
    system = _system_model(
        x3_system=x3_system,
        runner_status=runner_status,
        watchdog=watchdog,
        telegram_verify=telegram_verify,
        telegram_readiness=telegram_readiness,
        day_trade_qa=day_trade_qa,
        datatruth_latest=datatruth_latest,
        filltruth_manifest=filltruth_manifest,
        commit_manifest=commit_manifest,
        warnings=warnings,
    )
    no_picks = _no_picks_model(no_picks_raw)
    mission = _mission_model(
        app=app,
        calendar=calendar,
        days=days,
        strategies=strategies,
        intelligence=intelligence,
        system=system,
        no_picks=no_picks,
        day_trade_summary=day_trade_summary,
        open_positions=open_positions,
    )
    build_seed = {
        "latest_x3_generated_at": app.get("generated_at", "unknown"),
        "latest_x3_day": app.get("latest_run_date", "unknown"),
        "day_trade_build_id": day_trade_summary.get("build_id", "unknown"),
        "runner_build_id": runner_status.get("build_id", "unknown"),
        "market_masters_build_id": market_report.get("build_id", "unknown"),
        "source_ref_count": len(source_refs),
    }
    return InterfaceApexModel(
        mission=mission,
        calendar=calendar,
        days=days,
        strategies=strategies,
        trades=trades,
        intelligence=intelligence,
        system=system,
        no_picks=no_picks,
        source_refs=source_refs,
        warnings=_dedupe(warnings),
        build_seed=build_seed,
    )


def _mission_model(
    *,
    app: dict[str, Any],
    calendar: CalendarModel,
    days: list[DayModel],
    strategies: list[StrategyModel],
    intelligence: IntelligenceModel,
    system: SystemModel,
    no_picks: NoPicksModel,
    day_trade_summary: dict[str, Any],
    open_positions: Any,
) -> MissionModel:
    latest_day = days[-1] if days else None
    current_month = calendar.months[-1] if calendar.months else None
    day_trade_count = _as_int(day_trade_summary.get("total_day_trades"))
    overnight_count = _as_int(day_trade_summary.get("overnight_hold_count"))
    top_strategy = _top_day_trade_strategy(strategies)
    validation = str(day_trade_summary.get("strategy_validation", "not_validated"))
    status = str(app.get("overall_status") or system.sentinel_status or "unknown")
    headline = "Dawnstrike is running, with trust boundaries visible."
    if status in {"needs_attention", "warning"} or system.warnings:
        headline = "Dawnstrike is running, but it needs attention."
    if no_picks.accepted_count == 0:
        headline = "Dawnstrike is running and stayed disciplined today."
    subheadline = (
        f"The intraday research lane has {day_trade_count:,} historical same-session "
        f"trades and {overnight_count} overnight holds. Forward validation is still "
        f"{validation.replace('_', ' ')}."
    )
    next_action = "Watch the next scheduled run and keep forward evidence separate from research."
    if no_picks.accepted_count == 0:
        next_action = "Review why no official paper trades cleared the gates, then watch tomorrow."
    if system.warnings:
        next_action = "Resolve the top visible warning before treating any result as stronger evidence."
    open_count = len(open_positions) if isinstance(open_positions, list) else 0
    return MissionModel(
        headline=headline,
        subheadline=subheadline,
        status=status,
        latest_run_time=_first_text(app.get("generated_at"), system.sentinel_status, "unknown"),
        next_run_time=_next_run(system.scheduled_tasks),
        day_return=latest_day.daily_return if latest_day else "unknown",
        cumulative_return=(
            latest_day.cumulative_return
            if latest_day
            else current_month.cumulative_return_pct
            if current_month
            else "unknown"
        ),
        paper_trades_today=len(latest_day.trades) if latest_day else 0,
        open_paper_trades=open_count,
        top_strategy=top_strategy,
        top_warning=system.warnings[0] if system.warnings else "No critical warning in Apex source artifacts.",
        latest_lesson=intelligence.latest_lesson,
        next_action=next_action,
    )


def _calendar_model(months_raw: Any) -> CalendarModel:
    months: list[CalendarMonth] = []
    for raw in _list_of_dicts(months_raw):
        tiles: list[CalendarDayTile] = []
        for tile in _list_of_dicts(raw.get("calendar_days")):
            trade_count = _as_int(tile.get("trade_count"))
            warnings = _as_int(tile.get("warning_count"))
            date_text = str(tile.get("date", "unknown"))
            tiles.append(
                CalendarDayTile(
                    date=date_text,
                    daily_return_pct=_text(tile.get("daily_return_pct")),
                    cumulative_return_pct=_text(tile.get("cumulative_return_pct")),
                    trade_count=trade_count,
                    no_trade_marker=trade_count == 0 or str(tile.get("state")) == "no_trade",
                    warning_marker=warnings > 0,
                    learning_marker=_truthy(tile.get("has_learning")),
                    day_story_link=f"../days/{date_text}.html",
                    tone=str(tile.get("tone") or "flat"),
                )
            )
        months.append(
            CalendarMonth(
                month=str(raw.get("month", "unknown")),
                monthly_return_pct=_text(raw.get("monthly_return_pct")),
                cumulative_return_pct=_text(raw.get("cumulative_return_pct")),
                best_day=str(raw.get("best_day", "unknown")),
                worst_day=str(raw.get("worst_day", "unknown")),
                win_days=_as_int(raw.get("green_days")),
                loss_days=_as_int(raw.get("red_days")),
                no_trade_days=_as_int(raw.get("no_trade_days")),
                total_trades=sum(tile.trade_count for tile in tiles),
                previous_month=str(raw.get("previous_month") or raw.get("month") or "unknown"),
                next_month=str(raw.get("next_month") or raw.get("month") or "unknown"),
                day_tiles=tiles,
            )
        )
    return CalendarModel(
        months=months,
        current_month=months[-1].month if months else "unknown",
        day_tiles=[tile for month in months for tile in month.day_tiles],
    )


def _day_models(days_raw: Any, historical_examples: list[TradeModel]) -> list[DayModel]:
    examples_by_date: dict[str, list[TradeModel]] = {}
    for trade in historical_examples[:80]:
        examples_by_date.setdefault(trade.date, []).append(trade)
    days: list[DayModel] = []
    for raw in _list_of_dicts(days_raw):
        date_text = str(raw.get("date", "unknown"))
        paper_trades = [_paper_trade_model(date_text, item, index) for index, item in enumerate(_list_of_dicts(raw.get("paper_trades")))]
        sample_examples = examples_by_date.get(date_text, [])[:3]
        trades = paper_trades + sample_examples
        no_reasons = _string_list(raw.get("no_picks_reasons"))[:8]
        warnings = _string_list(raw.get("warnings"))[:12]
        summary = _day_summary(date_text=date_text, raw=raw, trades=trades, no_reasons=no_reasons)
        strategies = [
            str(row.get("strategy_id") or row.get("strategy_name"))
            for row in _list_of_dicts(raw.get("strategy_returns"))[:8]
        ]
        days.append(
            DayModel(
                date=date_text,
                headline=str(raw.get("headline") or f"{date_text}: Dawnstrike artifact story."),
                plain_english_summary=summary,
                daily_return=_text(_dict(raw.get("cumulative_returns")).get("daily_return_pct")),
                cumulative_return=_text(_dict(raw.get("cumulative_returns")).get("cumulative_return_pct")),
                trades=trades,
                strategies_evaluated=[item for item in strategies if item],
                no_picks_reasons=no_reasons,
                warnings=warnings,
                learning_note=str(raw.get("learning_foundry_lesson") or "No new lesson artifact for this day."),
                market_masters_note=str(raw.get("market_masters_lesson") or "No Market Masters update for this day."),
                evidence_quality=_evidence_quality(raw),
                what_to_watch_tomorrow=str(
                    raw.get("what_to_watch_next")
                    or "Watch whether official paper evidence appears without weakening safety gates."
                ),
            )
        )
    return sorted(days, key=lambda item: item.date)


def _ensure_calendar_day_models(*, days: list[DayModel], calendar: CalendarModel) -> list[DayModel]:
    by_date = {day.date: day for day in days}
    for tile in calendar.day_tiles:
        if tile.date in by_date:
            continue
        no_trade_reason = "No detailed day artifact exists for this calendar tile."
        if tile.no_trade_marker:
            no_trade_reason = "No official paper trade was recorded on this calendar tile."
        by_date[tile.date] = DayModel(
            date=tile.date,
            headline=f"{tile.date}: calendar tile available; detailed paper story is not present.",
            plain_english_summary=(
                "Apex generated this day story so every calendar tile remains clickable. "
                "The source calendar has return and trade-count data, but no deeper day artifact."
            ),
            daily_return=tile.daily_return_pct,
            cumulative_return=tile.cumulative_return_pct,
            trades=[],
            strategies_evaluated=[],
            no_picks_reasons=[no_trade_reason],
            warnings=["Detailed day artifact missing; values are limited to calendar tile evidence."],
            learning_note="No Learning Foundry lesson artifact was linked to this day.",
            market_masters_note="No Market Masters note artifact was linked to this day.",
            evidence_quality="calendar tile evidence only",
            what_to_watch_tomorrow="Watch the next generated day artifact before upgrading confidence.",
        )
    return sorted(by_date.values(), key=lambda item: item.date)


def _paper_trade_model(date_text: str, raw: dict[str, Any], index: int) -> TradeModel:
    symbol = str(raw.get("symbol") or "unknown")
    strategy = str(raw.get("strategy_id") or raw.get("strategy") or "unknown strategy")
    warnings = _string_list(raw.get("warnings"))
    reason = str(raw.get("reason") or "")
    if reason and not warnings:
        warnings = [reason[:220]]
    filltruth = str(raw.get("filltruth_certainty") or "unknown")
    commit_status = str(raw.get("commitbridge_status") or "unknown")
    official = "official paper evidence" if commit_status == "passed" else "paper evidence, not official"
    if filltruth in {"daily", "daily_only", "daily_approximation"}:
        warnings.append("Timing is daily-bar evidence; not proven as an intraday day trade.")
    return TradeModel(
        trade_id=f"paper-{date_text}-{strategy}-{symbol}-{index}",
        date=date_text,
        symbol=symbol,
        strategy=strategy,
        interval="paper evidence",
        entry_time="unknown",
        exit_time="unknown",
        hold_minutes="unknown",
        direction=str(raw.get("direction") or "unknown"),
        entry_price=_text(raw.get("entry") or raw.get("fill_price")),
        exit_price=_text(raw.get("close_price")),
        stop=_text(raw.get("stop")),
        target=_text(raw.get("target")),
        exit_reason=reason[:260] if reason else "paper result from artifact ledger",
        r_multiple=_text(raw.get("r_multiple")),
        pnl=_text(raw.get("realized_pnl")),
        evidence_type=f"paper artifact; fill quality {filltruth}",
        official_or_shadow=official,
        warnings=_dedupe(warnings)[:4],
    )


def _strategy_models(
    *,
    day_trade_comparison: Any,
    x3_strategies: Any,
    market_challengers: Any,
    fragility: dict[str, Any],
    oos: dict[str, Any],
    slippage: dict[str, Any],
    examples_by_strategy: dict[str, list[TradeModel]],
) -> list[StrategyModel]:
    models: list[StrategyModel] = []
    fragility_by_strategy = _fragility_by_strategy(fragility)
    oos_by_strategy = _oos_by_strategy(oos)
    slip_by_strategy = _slippage_by_strategy(slippage)
    seen: set[tuple[str, str]] = set()
    for raw in _list_of_dicts(day_trade_comparison):
        strategy_id = str(raw.get("strategy_id") or "unknown_day_trade")
        interval = str(raw.get("interval") or "intraday")
        model_key = (f"{strategy_id}_{interval}", "day_trade")
        if model_key in seen:
            continue
        seen.add(model_key)
        frag = fragility_by_strategy.get((strategy_id, interval), [])
        oos_row = oos_by_strategy.get((strategy_id, interval), {})
        slip_row = slip_by_strategy.get((strategy_id, interval), {})
        warnings = [
            "Historical intraday backtest only; not forward validation.",
            "No strategy is promoted to live trading by Apex.",
        ]
        warnings.extend(item.get("detail", item.get("reason", "")) for item in frag[:2])
        models.append(
            StrategyModel(
                strategy_id=f"{strategy_id}_{interval}",
                name=f"{_strategy_label(strategy_id)} ({interval})",
                lane="day_trade",
                status="historical intraday research - not validated",
                trade_count=_as_int(raw.get("trade_count")),
                win_rate=_pct_or_text(raw.get("win_rate")),
                average_r=_text(raw.get("average_r")),
                expectancy=_text(raw.get("expectancy")),
                profit_factor=_text(raw.get("profit_factor")),
                drawdown=_pct_or_text(raw.get("max_drawdown_pct")),
                robustness_score=_robustness_status(frag),
                slippage_status=str(slip_row.get("status") or slip_row.get("stress_status") or "stress artifact present"),
                oos_status=str(oos_row.get("overfit_warning") or oos_row.get("status") or "holdout reviewed"),
                validation_progress="not validated; needs forward same-session paper evidence",
                warnings=_dedupe([text for text in warnings if text])[:5],
                detail_link=f"../strategies/{_slug(strategy_id)}-{_slug(interval)}.html",
                trade_examples=examples_by_strategy.get(strategy_id, [])[:5],
                best_conditions=_condition_labels(raw.get("time_of_day_breakdown"), best=True),
                worst_conditions=_condition_labels(raw.get("day_of_week_breakdown"), best=False),
            )
        )
    for raw in _list_of_dicts(x3_strategies):
        strategy_id = str(raw.get("strategy_id") or "unknown_strategy")
        role = str(raw.get("role") or "research")
        lane = "benchmark" if role.lower() == "benchmark" else "swing_research"
        status = str(raw.get("status") or raw.get("latest_paper_state") or "artifact-backed research")
        models.append(
            StrategyModel(
                strategy_id=f"{strategy_id}_{lane}",
                name=str(raw.get("strategy_name") or _strategy_label(strategy_id)),
                lane=lane,
                status=f"daily-bar {status}",
                trade_count=_as_int(raw.get("trade_count")),
                win_rate=_pct_or_text(raw.get("win_rate")),
                average_r=_text(raw.get("average_r")),
                expectancy=_text(raw.get("expectancy")),
                profit_factor="unknown",
                drawdown=_text(raw.get("drawdown")),
                robustness_score="not an intraday robustness lane",
                slippage_status="not proven intraday",
                oos_status="not forward-validated as day trading",
                validation_progress=str(raw.get("validation_progress") or "not validated"),
                warnings=_dedupe(
                    ["Daily-bar swing research; not shown as day trading."]
                    + _string_list(raw.get("warnings"))[:4]
                ),
                detail_link=f"../strategies/{_slug(strategy_id)}-{lane}.html",
                trade_examples=[],
            )
        )
    for raw in _list_of_dicts(market_challengers.get("challengers") if isinstance(market_challengers, dict) else []):
        challenger_id = str(raw.get("challenger_id") or "shadow_challenger")
        models.append(
            StrategyModel(
                strategy_id=challenger_id,
                name=_strategy_label(challenger_id),
                lane="shadow_challenger",
                status=str(raw.get("status") or "shadow only - not promoted"),
                trade_count=0,
                win_rate="unknown",
                average_r="unknown",
                expectancy="unknown",
                profit_factor="unknown",
                drawdown="unknown",
                robustness_score="idea only",
                slippage_status="not tested for execution",
                oos_status="missing true forward sample",
                validation_progress="promotion blocked; shadow-only",
                warnings=["Shadow challenger; cannot replace an official paper strategy."],
                detail_link=f"../strategies/{_slug(challenger_id)}.html",
                trade_examples=[],
            )
        )
    lane_order = {"day_trade": 0, "swing_research": 1, "shadow_challenger": 2, "benchmark": 3}
    return sorted(models, key=lambda item: (lane_order.get(item.lane, 9), item.name))


def _intelligence_model(
    *,
    learning_verify: dict[str, Any],
    latest_lesson: dict[str, Any],
    latest_regime: dict[str, Any],
    market_report: dict[str, Any],
    market_challengers: dict[str, Any],
    market_methodologies: dict[str, Any],
    market_primitives: dict[str, Any],
) -> IntelligenceModel:
    lesson_parts = _string_list(latest_lesson.get("today_learned"))
    if not lesson_parts:
        lesson_parts = _string_list(latest_lesson.get("evidence_still_insufficient"))
    latest = lesson_parts[0] if lesson_parts else "Learning artifact says evidence is still insufficient."
    regime = ", ".join(
        item
        for item in [
            str(latest_regime.get("trend_regime") or ""),
            str(latest_regime.get("volatility_regime") or ""),
            str(latest_regime.get("risk_state") or ""),
        ]
        if item
    ) or "unknown"
    methodologies = [
        str(item.get("name") or item.get("methodology_id"))
        for item in _list_of_dicts(market_methodologies.get("methodologies"))[:6]
    ]
    primitives = [
        str(item.get("primitive_id") or item.get("output_signal"))
        for item in _list_of_dicts(market_primitives.get("primitives"))[:6]
    ]
    challenger_rows = _list_of_dicts(market_challengers.get("challengers"))
    challengers = [
        str(item.get("challenger_id") or item.get("rule_description")) for item in challenger_rows[:8]
    ]
    promotion_status = str(
        market_report.get("promotion_result")
        or latest_lesson.get("promotion_result")
        or "blocked_no_true_forward_sample"
    )
    return IntelligenceModel(
        learning_foundry_status=str(learning_verify.get("status") or latest_lesson.get("status") or "unknown"),
        latest_lesson=latest,
        regime=regime,
        market_masters_status=str(market_report.get("status") or "unknown"),
        methodologies=methodologies,
        primitives=primitives,
        challengers=challengers,
        promotion_status=promotion_status,
        shadow_only_count=len(challenger_rows),
        validation_blocked_reason=promotion_status.replace("_", " "),
    )


def _system_model(
    *,
    x3_system: dict[str, Any],
    runner_status: dict[str, Any],
    watchdog: dict[str, Any],
    telegram_verify: dict[str, Any],
    telegram_readiness: dict[str, Any],
    day_trade_qa: dict[str, Any],
    datatruth_latest: dict[str, Any],
    filltruth_manifest: dict[str, Any],
    commit_manifest: dict[str, Any],
    warnings: list[str],
) -> SystemModel:
    automation = _dict(x3_system.get("automation"))
    tasks = _list_of_dicts(runner_status.get("tasks"))
    provider_status = str(
        runner_status.get("autodata_provider_readiness_status")
        or datatruth_latest.get("status")
        or "unknown"
    )
    telegram_status = str(
        telegram_readiness.get("readiness_status")
        or telegram_verify.get("readiness_status")
        or telegram_verify.get("status")
        or automation.get("telegram_readiness")
        or "unknown"
    )
    data_status = str(datatruth_latest.get("status") or "artifact present")
    fill_status = str(filltruth_manifest.get("status") or "artifact present")
    commit_status = str(commit_manifest.get("status") or "artifact present")
    evidence_chain = f"Data quality {data_status}; fill quality {fill_status}; official gate {commit_status}"
    return SystemModel(
        scheduled_tasks=tasks,
        provider_status=provider_status,
        telegram_status=telegram_status,
        sentinel_status=str(runner_status.get("sentinel_status") or "unknown"),
        watchdog_status=str(watchdog.get("status") or runner_status.get("status") or "unknown"),
        data_quality_status=f"{data_status}; day-trade QA {day_trade_qa.get('status', 'unknown')}",
        evidence_chain_status=evidence_chain,
        live_trading_disabled=not _truthy(runner_status.get("live_trading_enabled")),
        secrets_safe=True,
        warnings=_dedupe(warnings)[:24],
    )


def _no_picks_model(raw: dict[str, Any]) -> NoPicksModel:
    return NoPicksModel(
        date=str(raw.get("date") or "unknown"),
        headline=str(
            raw.get("headline")
            or "No official paper trades today. That can be a disciplined result."
        ),
        accepted_count=_as_int(raw.get("accepted_count")),
        blocked_count=_as_int(raw.get("blocked_count")),
        watch_count=_as_int(raw.get("watch_count")),
        no_setup_count=_as_int(raw.get("no_setup_count")),
        top_reasons=_string_list(raw.get("top_reasons"))[:8],
        near_setups=_string_list(raw.get("near_setups"))[:8],
        strategies_blocked=_string_list(raw.get("strategies_blocked"))[:8],
        data_quality_blockers=_string_list(raw.get("data_quality_blockers"))[:8],
        riskhub_blockers=_string_list(raw.get("riskhub_blockers"))[:8],
        what_would_change=_string_list(raw.get("what_would_change"))[:8],
        why_no_trade_is_valid=str(
            raw.get("why_no_trade_is_valid")
            or "Standing aside is valid when evidence gates do not clear."
        ),
    )


def _load_day_trade_examples(day_trade_root: Path) -> list[TradeModel]:
    path = day_trade_root / "trades/corpus_day_trade_trades.csv"
    rows: list[TradeModel] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader):
            if index >= 180:
                break
            strategy = str(row.get("strategy_id") or "unknown")
            interval = str(row.get("interval") or "intraday")
            date_text = str(row.get("session_date") or row.get("date") or "unknown")
            overnight = str(row.get("overnight") or "").lower() == "true"
            is_day_trade = str(row.get("is_day_trade") or "").lower() == "true"
            warnings = ["Historical-only intraday research; not official paper evidence."]
            if not is_day_trade or overnight:
                warnings.append("Source row does not prove same-session flat status.")
            rows.append(
                TradeModel(
                    trade_id=f"hist-{strategy}-{interval}-{date_text}-{index}",
                    date=date_text,
                    symbol=str(row.get("symbol") or "unknown"),
                    strategy=strategy,
                    interval=interval,
                    entry_time=str(row.get("entry_time") or "unknown"),
                    exit_time=str(row.get("exit_time") or "unknown"),
                    hold_minutes=_text(row.get("hold_minutes")),
                    direction=str(row.get("direction") or "unknown"),
                    entry_price=_text(row.get("entry_price")),
                    exit_price=_text(row.get("exit_price")),
                    stop=_text(row.get("stop")),
                    target=_text(row.get("target")),
                    exit_reason=str(row.get("exit_reason") or "unknown"),
                    r_multiple=_text(row.get("r_multiple")),
                    pnl=_text(row.get("net_pnl") or row.get("gross_pnl")),
                    evidence_type=str(row.get("source_mode") or "historical_daytrade_backtest"),
                    official_or_shadow="historical-only",
                    warnings=warnings,
                )
            )
    return rows


def _trade_models_from_days(days: list[DayModel]) -> list[TradeModel]:
    trades: list[TradeModel] = []
    seen: set[str] = set()
    for day in sorted(days, key=lambda item: item.date, reverse=True):
        for trade in day.trades:
            if trade.trade_id in seen:
                continue
            seen.add(trade.trade_id)
            trades.append(trade)
    return trades


def _group_trades_by_strategy(trades: list[TradeModel]) -> dict[str, list[TradeModel]]:
    grouped: dict[str, list[TradeModel]] = {}
    for trade in trades:
        grouped.setdefault(trade.strategy, []).append(trade)
    return grouped


def _fragility_by_strategy(raw: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _list_of_dicts(raw.get("rows")):
        grouped.setdefault((str(row.get("strategy_id")), str(row.get("interval"))), []).append(row)
    return grouped


def _oos_by_strategy(raw: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _list_of_dicts(raw.get("rows")):
        grouped.setdefault((str(row.get("strategy_id")), str(row.get("interval"))), row)
    return grouped


def _slippage_by_strategy(raw: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _list_of_dicts(raw.get("stress_rows")):
        grouped.setdefault((str(row.get("strategy_id")), str(row.get("interval"))), row)
    return grouped


def _robustness_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "robustness artifact present"
    severities = {str(row.get("severity") or "warning") for row in rows}
    if "high" in severities or "critical" in severities:
        return "fragile in some slices"
    return "watch listed for fragility"


def _condition_labels(value: Any, *, best: bool) -> list[str]:
    rows = _list_of_dicts(value)
    if not rows:
        return ["unknown"]
    key = "average_pnl"
    ordered = sorted(rows, key=lambda row: _as_float(row.get(key)), reverse=best)
    labels: list[str] = []
    for row in ordered[:3]:
        label = (
            row.get("time_bucket")
            or row.get("day_of_week")
            or row.get("symbol")
            or row.get("interval")
            or "slice"
        )
        labels.append(f"{label}: {row.get(key, 'unknown')}")
    return labels or ["unknown"]


def _top_day_trade_strategy(strategies: list[StrategyModel]) -> str:
    for strategy in strategies:
        if strategy.lane == "day_trade":
            return strategy.name
    return "unknown"


def _next_run(tasks: list[dict[str, Any]]) -> str:
    future = sorted(
        [
            str(task.get("next_run_time"))
            for task in tasks
            if task.get("next_run_time") and str(task.get("next_run_time")) != "None"
        ]
    )
    return future[0] if future else "unknown"


def _day_summary(
    *, date_text: str, raw: dict[str, Any], trades: list[TradeModel], no_reasons: list[str]
) -> str:
    if trades:
        paper_count = len([trade for trade in trades if trade.official_or_shadow.startswith("official")])
        historical_count = len([trade for trade in trades if trade.official_or_shadow == "historical-only"])
        pieces = []
        if paper_count:
            pieces.append(f"{paper_count} official paper-evidence card(s) were present.")
        if historical_count:
            pieces.append(f"{historical_count} historical day-trade example(s) are shown only as research context.")
        return " ".join(pieces)
    if no_reasons:
        return f"{date_text} had no official paper trades because {no_reasons[0]}"
    return str(raw.get("market_context") or "No official paper trade fired in the available artifacts.")


def _evidence_quality(raw: dict[str, Any]) -> str:
    fill = str(raw.get("filltruth_summary") or "fill quality unknown")
    commit = str(raw.get("commitbridge_summary") or "official gate unknown")
    return f"{fill}; {commit}"


def _source_refs(repo_root: Path, paths: list[Path]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for path in paths:
        if path.is_dir():
            exists = path.exists()
            kind = "directory"
        else:
            exists = path.exists()
            kind = path.suffix.lstrip(".") or "artifact"
        try:
            display = path.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            display = path.as_posix()
        refs.append({"path": display, "exists": exists, "kind": kind})
    return refs


def _artifact_warnings(refs: list[dict[str, Any]]) -> list[str]:
    warnings = []
    for ref in refs:
        if not ref.get("exists"):
            warnings.append(f"Missing artifact: {ref.get('path')}")
    return warnings


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _latest_json(folder: Path, pattern: str) -> dict[str, Any]:
    if not folder.exists():
        return {}
    candidates = sorted(folder.glob(pattern))
    if not candidates:
        return {}
    data = _read_json(candidates[-1], {})
    return data if isinstance(data, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [part.strip() for part in value.split("|") if part.strip()]
    return []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _text(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    return str(value)


def _first_text(*values: Any) -> str:
    for value in values:
        if value not in {None, "", "unknown"}:
            return str(value)
    return "unknown"


def _pct_or_text(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, int | float):
        return f"{value:.2%}" if abs(value) <= 1 else f"{value:.2f}%"
    return str(value)


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value).replace("%", "")))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "passed", "ready", "ok"}


def _strategy_label(strategy_id: str) -> str:
    words = strategy_id.replace("_", " ").replace("-", " ").split()
    return " ".join(word.upper() if word in {"orb", "vwap"} else word.capitalize() for word in words)


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
