"""Compact Telegram message formatting for operator notifications."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from intraday_scanner.notifiers.base import NotificationEvent
from intraday_scanner.services.time_utils import get_operator_time_label

DEFAULT_MORNING_MAX_CHARS = 4096
DEFAULT_ALERT_MAX_CHARS = 4096
DEFAULT_SUMMARY_MAX_CHARS = 4096

CANONICAL_COHORT_LABELS = {
    "official_forward_paper": "Official paper",
    "alphaops_signal_research": "Research observations",
    "historical_backtest": "Historical backtest",
    "shadow_challenger": "Shadow challenger",
}


def format_telegram_event(
    event: NotificationEvent,
    *,
    max_morning_chars: int = DEFAULT_MORNING_MAX_CHARS,
    max_alert_chars: int = DEFAULT_ALERT_MAX_CHARS,
    max_summary_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    include_debug_fields: bool = False,
) -> str:
    payload = dict(event.payload or {})
    compact = str(payload.get("telegram_compact_message") or "").strip()
    limit = _limit_for_hint(
        event.channel_hint,
        max_morning_chars,
        max_alert_chars,
        max_summary_chars,
    )
    if compact and not include_debug_fields:
        return _clip(compact, limit)
    body = event.body.strip()
    if _looks_compact(body) and not include_debug_fields:
        return _clip(body, limit)
    text = f"{event.title}\n{body}".strip()
    if include_debug_fields and payload:
        debug = {key: value for key, value in payload.items() if "token" not in key.lower()}
        text = f"{text}\n\nDebug: {debug}"
    return _clip(text, limit)


def format_morning_watchlist(
    *,
    ranked: list[dict[str, Any]],
    avoid: list[dict[str, Any]],
    source_summary: dict[str, Any],
    timezone: str = "America/Chicago",
    max_chars: int = DEFAULT_MORNING_MAX_CHARS,
) -> str:
    picks = ranked[:3]
    lines = [
        "🚀 Dawnstrike Watchlist",
        (
            f"⏱ {get_operator_time_label(timezone)} | {len(ranked[:3])} picks | "
            f"Source: {_source_label(source_summary)}"
        ),
        "",
    ]
    if not picks:
        lines.extend(["No saved picks found.", "", "Research only. No orders placed."])
        return _clip("\n".join(lines), max_chars)
    for index, row in enumerate(picks, start=1):
        ticker = _text(row.get("ticker"), "n/a")
        catalyst = _catalyst_line(row)
        lines.append(
            f"{index}) {ticker} — {format_score(row.get('score'))} | "
            f"{format_percent(row.get('gap_pct'))} | {format_price(row.get('premarket_price'))}"
        )
        lines.append(
            f"   🎯 {format_price(row.get('breakout_trigger') or row.get('target_1'))} | "
            f"🛑 {format_price(row.get('invalidation_level') or row.get('invalidation'))}"
        )
        if catalyst != "none":
            lines.append(f"   📰 {catalyst}")
        risk = _risk_text(row)
        if risk != "none":
            lines.append(f"   ⚠️ {_truncate(risk, 80)}")
        lines.append("")
    extra = len(ranked) - len(picks)
    if extra > 0:
        lines.append(f"+{extra} more in dashboard.")
        lines.append("")
    if avoid:
        lines.append(f"🚫 Avoid: {len(avoid)}")
        lines.append("")
    lines.append("Research only. No orders placed.")
    return _clip("\n".join(lines).strip(), max_chars)


def format_risk_alert(row: dict[str, Any]) -> str:
    ticker = _text(row.get("ticker"), "UNKNOWN")
    reason = _risk_text(row)
    return "\n".join(
        [
            "⚠️ Dawnstrike Alert",
            f"{ticker} — CAUTION",
            f"Reason: {reason}",
            "Action: manual review",
            "No orders placed.",
        ]
    )


def format_manual_monitor(tickers: list[str]) -> str:
    watch = ", ".join(ticker for ticker in tickers if ticker) or "No saved picks found."
    return "\n".join(
        [
            "👀 Manual Monitor Needed",
            "No live price source configured.",
            f"Watch: {watch}",
        ]
    )


def format_outcome_needed(
    *,
    run_date: str,
    reminder_path: str,
    tickers: list[str],
) -> str:
    lines = [
        "📥 Outcome Data Needed",
        "Save:",
        reminder_path or f"data\\inbox\\outcomes\\outcomes_{run_date}.csv",
        "",
        "Tickers:",
        ", ".join(tickers) if tickers else "No saved picks found.",
        "",
        "Needed:",
        "entry, 1m, 5m, 15m, lunch, close, high, low",
    ]
    return "\n".join(lines)


def format_daily_summary(summary: dict[str, Any]) -> str:
    report = dict(summary.get("shadow_report") or {})
    return "\n".join(
        [
            "📊 Dawnstrike Summary",
            f"Top1: {format_percent(report.get('top_1_close_return_pct'), signed=False)}",
            f"Top3: {format_percent(report.get('top_3_close_return_pct'), signed=False)}",
            f"Top5: {format_percent(report.get('top_5_close_return_pct'), signed=False)}",
            f"Missing outcomes: {_text(summary.get('missing_outcome_count'), 'n/a')}",
            f"Dashboard: {_text(summary.get('dashboard_url'), 'http://127.0.0.1:8502/')}",
            "",
            "Manual/free shadow results only.",
        ]
    )


def format_canonical_daily_performance(
    row: dict[str, Any],
    *,
    max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> str:
    """Format one canonical daily row without recomputing any metric."""

    cohort = CANONICAL_COHORT_LABELS.get(
        str(row.get("cohort") or ""), str(row.get("cohort") or "Not reported")
    )
    coverage = dict(row.get("coverage") or {})
    coverage_text = (
        "not reported"
        if coverage.get("coverage_pct") is None
        else f"{float(coverage['coverage_pct']):.1f}%"
    )
    lines = [
        "📊 Dawnstrike Canonical Performance",
        f"Cohort: {cohort}",
        f"ID: {_text(row.get('performance_id'), 'not reported')}",
        (
            f"Date: {_text(row.get('market_date'), 'not reported')} "
            f"({_text(row.get('timezone'), 'timezone not reported')})"
        ),
        (
            f"Daily: {_canonical_percent(row.get('return_pct'))} | "
            f"Cumulative: {_canonical_percent(row.get('cumulative_return_pct'))}"
        ),
        (
            f"Benchmark: {_canonical_percent(row.get('benchmark_return_pct'))} | "
            f"Excess: {_canonical_percent(row.get('excess_return_pct'))}"
        ),
        (
            f"Net P&L: {_canonical_money(row.get('net_pnl_cents'))} | "
            f"Drawdown: {_canonical_percent(row.get('drawdown_pct'))}"
        ),
        (
            f"Basis: {_text(row.get('return_basis'), 'not reported')} | "
            f"Costs: {_text(row.get('cost_status'), 'not reported')}"
        ),
        (
            f"Coverage: {coverage_text} | "
            f"Evidence: {_text(row.get('evidence_state'), 'not reported')}"
        ),
        (
            f"Input: {_short_hash(row.get('input_hash_sha256'))} | "
            f"Sources: {len(row.get('source_refs') or [])}"
        ),
        f"As of: {_text(row.get('generated_at') or row.get('calculated_at'), 'not reported')}",
        "Research only. No orders placed.",
    ]
    return _clip("\n".join(lines), max_chars)


def format_source_check(source_summary: dict[str, Any]) -> str:
    attempts = list(source_summary.get("attempts") or [])
    top_reason = _text(source_summary.get("top_failure_reason"), "")
    lines = [
        "📡 Dawnstrike Source Check",
        "No usable rows found.",
    ]
    if top_reason:
        lines.append(f"Top reason: {top_reason.replace('_', ' ')}")
    lines.extend(["", "Tried:"])
    if not attempts:
        lines.append("- no enabled candidate sources")
    else:
        for attempt in attempts[:5]:
            source = _attempt_label(attempt)
            status = str(attempt.get("status") or attempt.get("reason") or "unknown")
            if status == "failed":
                status = str(attempt.get("reason") or attempt.get("failure_reason") or "failed")
            lines.append(f"- {source}: {status.replace('_', ' ')}")
    lines.extend(
        [
            "",
            "Next:",
            "Try again during premarket or drop CSV into data\\inbox\\screener.",
        ]
    )
    return "\n".join(lines)


def format_alpha_watch(
    *,
    signals: list[dict[str, Any]],
    edge_label: str,
    source_summary: dict[str, Any] | None = None,
    blocked_signals: list[dict[str, Any]] | None = None,
    timezone: str = "America/Chicago",
    max_chars: int = DEFAULT_MORNING_MAX_CHARS,
) -> str:
    source_summary = dict(source_summary or {})
    blocked_signals = list(blocked_signals or [])
    candidates = [row for row in signals if _telegram_candidate_allowed(row)][:5]
    slate = dict(source_summary.get("ranked_research_slate") or {})
    slate_total = int(slate.get("published_count") or len(candidates))
    slate_shown = len(candidates)
    official_candidates = [
        row
        for row in candidates
        if (
            str(row.get("publication_tier") or "")
            in {"PAPER_PLAN_QUALIFIED", "ALERTABLE_PAPER_ENTRY"}
            or (
                not row.get("publication_tier")
                and str(row.get("decision_tier") or "").lower() == "clean_edge"
            )
        )
        and str(row.get("alert_gate_status") or "").upper() in {"PASS", "ALERT_OK"}
        and row.get("manual_confirmation_required") is False
    ][:3]
    research_watchlist = [row for row in candidates if row not in official_candidates][:3]
    explicit_publication = any("publication_tier" in row for row in candidates)
    official_heading = (
        "OFFICIAL PAPER CANDIDATES"
        if official_candidates or not explicit_publication
        else "PAPER PLAN QUALIFIED"
    )
    lines = [
        "🚀 Dawnstrike Alpha Watch",
        (
            f"⏱ {get_operator_time_label(timezone)} | Edge: {edge_label} | "
            f"{len(official_candidates)} official candidates | "
            f"{len(research_watchlist)} research"
        ),
        "",
        f"Research slate: {slate_shown} of {slate_total} shown",
        "",
        official_heading,
        "(pending fresh quote, session, cost, chase, and portfolio checks)",
    ]
    if not official_candidates:
        lines.append("- None")
    for index, row in enumerate(official_candidates, start=1):
        lines.append(
            f"{index}) {_text(row.get('ticker'), 'n/a')} — Alpha "
            f"{format_score(row.get('alpha_score'))} | "
            f"{_text(row.get('review_label') or row.get('edge_bucket'), 'n/a')} | "
            f"Tier {_text(row.get('publication_tier'), 'OFFICIAL')}"
        )
        lines.append(
            f"   Trigger {format_price(row.get('entry_trigger') or row.get('breakout_trigger'))} | "
            f"Invalid {format_price(row.get('invalidation') or row.get('invalidation_level'))} | "
            f"Target {format_price(row.get('target_1') or row.get('first_target'))}"
        )
        lines.append(
            f"   Confidence {_text(row.get('confidence_bucket'), 'n/a')} | "
            f"Setup {_text(row.get('setup_key'), 'n/a')}"
        )
        for receipt_line in _decision_receipt_lines(row):
            lines.append(f"   {receipt_line}")
        risk = _risk_text(row)
        if risk != "none":
            lines.append(f"   Risk {_truncate(risk, 80)}")
    lines.extend(["", "RESEARCH WATCHLIST"])
    if not research_watchlist:
        lines.append("- None")
    for row in research_watchlist:
        reasons = (
            row.get("alert_gate_reasons")
            or row.get("public_data_warning")
            or row.get("no_trade_reason")
            or "V5 official-paper checks not satisfied"
        )
        if isinstance(reasons, list):
            reasons = "; ".join(str(item) for item in reasons)
        lines.append(
            f"- {_text(row.get('ticker'), 'n/a')}: "
            f"Tier {_text(row.get('publication_tier'), 'RANKED_RESEARCH_CANDIDATE')} | "
            f"{_truncate(str(reasons), 100)}"
        )
        for receipt_line in _decision_receipt_lines(row):
            lines.append(f"  {receipt_line}")
    lines.extend(["", "NO TRADE / BLOCKED REASONS"])
    if blocked_signals:
        for row in blocked_signals[:3]:
            reason = (
                row.get("no_trade_reason")
                or row.get("alert_gate_reasons")
                or row.get("avoid_reasons")
                or row.get("risk_flags")
                or "blocked by deterministic policy"
            )
            if isinstance(reason, list):
                reason = "; ".join(str(item) for item in reason)
            lines.append(f"- {_text(row.get('ticker'), 'n/a')}: {_truncate(str(reason), 100)}")
            for receipt_line in _decision_receipt_lines(row):
                lines.append(f"  {receipt_line}")
    else:
        lines.append(
            "- "
            + _text(
                source_summary.get("top_failure_reason"),
                "No additional blocked rows",
            )
        )
    if not official_candidates and not research_watchlist:
        lines.extend(["", "No clean edge today."])
    lines.append("")
    lines.append("No orders placed. Research only.")
    return _clip("\n".join(lines).strip(), max_chars)


def format_alpha_no_trade(
    *,
    reason: str,
    next_action: str,
    research_signals: list[dict[str, Any]] | None = None,
    research_total: int | None = None,
    max_chars: int = DEFAULT_ALERT_MAX_CHARS,
) -> str:
    radar = list(research_signals or [])[:3]
    total = int(research_total if research_total is not None else len(research_signals or []))
    lines = [
        "📡 Dawnstrike Alpha Check",
        "No clean edge today.",
        "",
        f"Research slate: {len(radar)} of {total} shown",
        "",
        (
            "OFFICIAL PAPER CANDIDATES"
            if any(
                str(row.get("publication_tier") or "")
                in {"PAPER_PLAN_QUALIFIED", "ALERTABLE_PAPER_ENTRY"}
                for row in radar
            )
            or not any("publication_tier" in row for row in radar)
            else "PAPER PLAN QUALIFIED"
        ),
        "- None",
        "",
        "RESEARCH WATCHLIST / RADAR — CONDITIONAL PAPER STUDY",
    ]
    if not radar:
        lines.append("- None passed the liquid 1.5R research floor")
    for index, row in enumerate(radar, start=1):
        radar_target = row.get("radar_target") or row.get("target_1") or row.get("first_target")
        lines.append(
            f"{index}) {_text(row.get('ticker'), 'n/a')} — Alpha "
            f"{format_score(row.get('alpha_score'))} | "
            f"Tier {_text(row.get('publication_tier'), 'RANKED_RESEARCH_CANDIDATE')} | "
            f"Gap {format_percent(row.get('gap_pct'))} | "
            f"{format_score(row.get('reward_risk_ratio'))}R"
        )
        lines.append(
            f"   Trigger {format_price(row.get('entry_trigger') or row.get('breakout_trigger'))} | "
            f"Invalid {format_price(row.get('invalidation') or row.get('invalidation_level'))} | "
            f"Target {format_price(radar_target)}"
        )
        lines.append(
            "   Why research-only: "
            + _truncate(
                str(row.get("radar_reason") or "official evidence gate incomplete"),
                110,
            )
        )
    lines.extend(
        [
            "",
            "NO TRADE / BLOCKED REASONS",
            f"- {reason}",
            f"Next: {next_action}",
            "",
            "Radar outcomes are tracked after close. No orders placed.",
        ]
    )
    return _clip(
        "\n".join(lines),
        max_chars,
    )


def format_alpha_monitor(result: dict[str, Any], max_chars: int = DEFAULT_ALERT_MAX_CHARS) -> str:
    if result.get("status") == "manual_monitor_required":
        tickers = ", ".join(str(item) for item in result.get("tickers") or []) or "none"
        return _clip(
            "\n".join(
                [
                    "👀 Dawnstrike Alpha Monitor",
                    "MANUAL REVIEW",
                    "No live/current price source configured.",
                    f"Watch: {tickers}",
                    "",
                    "No orders placed. Research only.",
                ]
            ),
            max_chars,
        )
    events = list(result.get("events") or [])
    lines = ["👀 Dawnstrike Alpha Monitor"]
    for event in events[:5]:
        lines.append(
            f"{_text(event.get('ticker'), 'n/a')}: {_text(event.get('label'), 'MANUAL REVIEW')} "
            f"at {format_price(event.get('current_price'))}"
        )
        lines.append(
            f"   Trigger {format_price(event.get('entry_trigger'))} | "
            f"Invalid {format_price(event.get('invalidation_level'))} | "
            f"Target {format_price(event.get('target_1'))}"
        )
        if event.get("spread_pct") not in {None, ""}:
            lines.append(f"   Live spread {format_percent(event.get('spread_pct'))}")
    if not events:
        lines.append("No active events.")
    lines.extend(["", "No orders placed. Research only."])
    return _clip("\n".join(lines), max_chars)


def format_alpha_summary(
    summary: dict[str, Any],
    max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> str:
    truth = dict(summary.get("truth_report") or summary)
    sample = int(truth.get("real_days_collected") or 0)
    warning = " — insufficient sample" if sample < 20 else ""
    lines = [
        "📊 Dawnstrike Shadow Results",
        f"Top1 avg: {format_percent(dict(truth.get('top1') or {}).get('avg_return_pct'))}",
        f"Top3 avg: {format_percent(dict(truth.get('top3') or {}).get('avg_return_pct'))}",
        f"Top5 avg: {format_percent(dict(truth.get('top5') or {}).get('avg_return_pct'))}",
        f"Win rate: {format_percent(truth.get('win_rate_pct'), signed=False)}",
        f"Sample: {sample} days{warning}",
        "",
        "No orders placed. Research only.",
    ]
    return _clip("\n".join(lines), max_chars)


def format_score(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number:.1f}"


def format_percent(value: Any, *, signed: bool = True) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.0f}%"


def format_price(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"${number:.2f}"


def _canonical_percent(value: Any) -> str:
    number = _number_or_none(value)
    return "not reported" if number is None else f"{number:+.2f}%"


def _canonical_money(value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return "not reported"
    sign = "+" if number >= 0 else "-"
    return f"{sign}${abs(number) / 100:.2f}"


def _short_hash(value: Any) -> str:
    text = str(value or "")
    return f"{text[:10]}…" if text else "not reported"


def _number_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def format_dollar_volume(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    abs_value = abs(number)
    if abs_value >= 1_000_000_000:
        return f"${number / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def _source_label(source_summary: dict[str, Any]) -> str:
    attempts = list(source_summary.get("attempts") or [])
    if any(
        str(item.get("source") or "").lower() == "local_inbox" and item.get("status") == "success"
        for item in attempts
    ):
        return "manual"
    if attempts:
        return "web"
    return "manual/web"


def _attempt_label(attempt: dict[str, Any]) -> str:
    source_type = str(attempt.get("source_type") or "")
    source = str(attempt.get("source") or "")
    if source_type == "local_inbox" or source == "local_inbox":
        return "local inbox"
    if source:
        return source
    return source_type or "source"


def _risk_text(row: dict[str, Any]) -> str:
    raw = row.get("risk_flags") or row.get("avoid_reasons") or ""
    if isinstance(raw, list):
        raw = ", ".join(str(item) for item in raw if item)
    text = str(raw or "").strip().strip(";")
    return text if text else "none"


def _action_parts(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", "CAUTION"
    return "", text


def _decision_receipt_lines(row: dict[str, Any]) -> list[str]:
    tier = str(row.get("pick_tier") or "").strip()
    receipt_id = str(row.get("receipt_id") or "").strip()
    if not tier and not receipt_id and not row.get("strategy_receipt_gap"):
        return []
    strategy_id = _text(row.get("strategy_id"), "not reported")
    strategy_version = _text(
        row.get("strategy_version") or row.get("model_version"), "not reported"
    )
    entry = (
        "entry confirmation required"
        if not row.get("paper_entry_eligible")
        else "paper-entry conditions passed"
    )
    why = _text(
        row.get("receipt_reason")
        or row.get("why_qualified")
        or row.get("first_blocking_failure")
        or row.get("strategy_receipt_gap"),
        "not reported",
    )
    lines = [
        f"Receipt {_text(receipt_id, 'not recorded')} | Tier {_text(tier, 'not reported')}",
        (
            f"Strategy {strategy_id} {strategy_version} | "
            f"R/R {format_score(row.get('reward_risk_ratio'))}R"
        ),
        f"Entry: {entry}",
        f"Why: {_truncate(why, 100)}",
    ]

    core = row.get("core_conditions_passed") or []
    if isinstance(core, (list, tuple)):
        core_items: list[str] = []
        for item in core[:6]:
            value = item.get("condition_id") if isinstance(item, dict) else item
            if str(value or "").strip():
                core_items.append(str(value))
        core_text = ", ".join(core_items)
    else:
        core_text = str(core).strip()
    lines.append(f"Core passed: {_truncate(core_text, 100) if core_text else 'not reported'}")

    ai = row.get("ai_resolved_evidence") or []
    ai_parts: list[str] = []
    if isinstance(ai, (list, tuple)):
        for item in ai[:3]:
            if not isinstance(item, dict):
                ai_parts.append(str(item))
                continue
            condition_id = _text(item.get("condition_id"), "condition")
            urls = item.get("source_urls") or []
            citation = (
                _citation_label(urls[0])
                if isinstance(urls, list) and urls
                else "citation not reported"
            )
            ai_parts.append(f"{condition_id} [{citation}]")
    ai_text = _truncate("; ".join(ai_parts), 120) if ai_parts else "none reported"
    lines.append(f"AI evidence: {ai_text}")

    missing = row.get("disclosed_gaps") or row.get("first_blocking_failure") or ""
    if isinstance(missing, (list, tuple)):
        missing = ", ".join(str(item) for item in missing[:4])
    lines.append(f"Gaps: {_truncate(str(missing), 100) if missing else 'none'}")
    return lines


def _decision_receipt_line(row: dict[str, Any]) -> str:
    """Compatibility helper for callers that need one compact receipt line."""

    return " | ".join(_decision_receipt_lines(row))


def _citation_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "citation not reported"
    parsed = urlsplit(text)
    if parsed.netloc:
        text = parsed.netloc + (parsed.path or "")
    return _truncate(text, 48)


def _catalyst_line(row: dict[str, Any]) -> str:
    summary = _text(
        row.get("catalyst_summary") or row.get("catalyst_headline"),
        "none",
    )
    if summary.lower() in {"no clear catalyst", "none"}:
        return "none"
    return _truncate(summary, 60)


def _issue_count(ranked: list[dict[str, Any]], avoid: list[dict[str, Any]]) -> int:
    warnings = 0
    for row in ranked:
        raw = str(row.get("data_warnings") or "").strip()
        if raw:
            warnings += len([part for part in raw.replace(",", ";").split(";") if part.strip()])
    return warnings + len(avoid)


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not text or text.lower() in {"n/a", "na", "none"}:
        return None
    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b"}:
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _telegram_candidate_allowed(row: dict[str, Any]) -> bool:
    tier = str(row.get("publication_tier") or "")
    if tier:
        return tier in {
            "RANKED_RESEARCH_CANDIDATE",
            "PAPER_PLAN_QUALIFIED",
            "WAITING_CURRENT_CHECKS",
            "ALERTABLE_PAPER_ENTRY",
        }
    if not row.get("can_alert"):
        return False
    for key in (
        "hard_avoid_reasons",
        "hard_veto_reasons",
        "hard_no_trade_reason",
        "stale",
        "stale_data_flag",
        "fabricated",
        "is_fabricated",
        "unsafe",
    ):
        value = row.get(key)
        if (
            value is True
            or (isinstance(value, str) and value.strip())
            or (isinstance(value, (list, tuple, set)) and any(str(item).strip() for item in value))
        ):
            return False
    return True


def _text(value: Any, default: str = "n/a") -> str:
    text = str(value or "").strip()
    return text if text else default


def _truncate(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else value[: max_chars - 3].rstrip() + "..."


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _looks_compact(body: str) -> bool:
    return body.startswith(("🚀", "⚠️", "👀", "📥", "📊", "📡"))


def _limit_for_hint(
    channel_hint: str,
    max_morning_chars: int,
    max_alert_chars: int,
    max_summary_chars: int,
) -> int:
    if channel_hint in {"top_picks", "web_auto_pilot"}:
        return max_morning_chars
    if channel_hint == "daily_summary":
        return max_summary_chars
    return max_alert_chars
