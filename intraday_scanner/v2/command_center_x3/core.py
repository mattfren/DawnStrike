"""Command Center X3 story-first static UI rendering."""

# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
from calendar import monthrange
from pathlib import Path
from typing import Any

from intraday_scanner.v2.command_center_x2.adapters import build_story_bundle
from intraday_scanner.v2.command_center_x2.story_models import to_plain
from intraday_scanner.v2.command_center_x3.qa import REQUIRED_PAGE_NAMES, run_command_center_x3_qa

OUTPUT_DIRS = (
    "pages",
    "days",
    "months",
    "strategies",
    "assets",
    "data",
    "reports",
    "qa",
    "manifests",
)

PRIMARY_NAV = (
    ("Home", "pages/home.html"),
    ("Calendar", "pages/calendar.html"),
    ("Strategies", "pages/strategies.html"),
    ("Trades", "pages/trades.html"),
    ("System", "pages/system.html"),
)

COPY_TRANSLATIONS = {
    "AutoData": "Market data connection",
    "DataTruth": "Data quality check",
    "FillTruth": "Would this trade have filled?",
    "CommitBridge": "Can this paper result be trusted officially?",
    "PaperOps": "Paper trading record",
    "RiskHub": "Risk filter",
    "Learning Foundry": "What Dawnstrike learned",
    "Market Masters": "Research-inspired strategy ideas",
    "Strategy Evidence": "Strategy report card",
    "Command Center": "Dashboard",
}

SECRET_TERMS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
    "TWELVE_DATA_API_KEY",
)


def _clean_generated_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


def build_command_center_x3(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x3"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    data = _story_payload(repo_root=repo_root)
    _write_data(output_root=output_root, data=data)
    _write_assets(output_root)
    build_id = _stable_build_id(data)
    pages: list[Path] = []
    pages.extend(_render_primary_pages(output_root=output_root, data=data, build_id=build_id))
    pages.extend(_render_month_pages(output_root=output_root, data=data, build_id=build_id))
    pages.extend(_render_day_pages(output_root=output_root, data=data, build_id=build_id))
    pages.extend(_render_strategy_pages(output_root=output_root, data=data, build_id=build_id))
    manifest = {
        "schema_version": "v2.command_center_x3.manifest.v1",
        "status": "passed",
        "final_status": "BUILT_COMMAND_CENTER_X3",
        "build_id": build_id,
        "index": (output_root / "index.html").as_posix(),
        "page_count": len({path.as_posix() for path in pages}),
        "top_level_nav_count": len(PRIMARY_NAV),
        "day_count": len(data.get("days", [])),
        "month_count": len(data.get("months", [])),
        "strategy_count": len(_strategy_groups(data)),
        "research_only": True,
        "live_trading_enabled": False,
        "x2_preserved": (repo_root / "data/v2_command_center_x2/index.html").exists(),
        "pages": [path.as_posix() for path in sorted(set(pages), key=lambda item: item.as_posix())],
    }
    _write_json(output_root / "manifests/command_center_x3_manifest.json", manifest)
    _write_json(output_root / "reports/build_report.json", manifest)
    (output_root / "reports/build_report.md").write_text(_build_report_md(manifest), encoding="utf-8", newline="\n")
    return manifest


def qa_command_center_x3(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x3"),
) -> dict[str, Any]:
    return run_command_center_x3_qa(output_root=output_root, repo_root=repo_root)


def verify_command_center_x3(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x3"),
) -> dict[str, Any]:
    qa = qa_command_center_x3(repo_root=repo_root, output_root=output_root)
    manifest = _read_json(output_root / "manifests/command_center_x3_manifest.json", {})
    required_docs = [
        "docs/architecture/v2_command_center_x3.md",
        "docs/operations/command_center_x3_user_guide.md",
        "docs/audit/omega_command_center_x3_release_summary.md",
        "docs/audit/omega_command_center_x3_quality_scorecard.md",
        "docs/audit/omega_command_center_x3_red_team.md",
        "docs/audit/omega_command_center_x3_build_state.json",
        "docs/audit/omega_command_center_x3_resume_goal.md",
    ]
    missing_docs = [path for path in required_docs if not (repo_root / path).exists()]
    missing_pages = [
        name for name in REQUIRED_PAGE_NAMES if not (output_root / "pages" / name).exists()
    ]
    failures: list[str] = []
    if qa.get("status") != "passed":
        failures.append("qa_not_passed")
    if int(manifest.get("top_level_nav_count") or 99) > 6:
        failures.append("too_many_top_level_nav_items")
    if int(manifest.get("day_count") or 0) <= 0:
        failures.append("day_pages_missing")
    if int(manifest.get("month_count") or 0) <= 0:
        failures.append("month_pages_missing")
    if missing_pages:
        failures.append("missing_required_pages")
    if missing_docs:
        failures.append("missing_required_docs")
    if not manifest.get("x2_preserved"):
        failures.append("x2_not_preserved")
    result = {
        "schema_version": "v2.command_center_x3.verify.v1",
        "status": "passed" if not failures else "failed",
        "qa_status": qa.get("status", "missing"),
        "failures": failures,
        "missing_docs": missing_docs,
        "missing_pages": missing_pages,
        "manifest": manifest,
    }
    _write_json(output_root / "reports/verify_latest.json", result)
    (output_root / "reports/verify_latest.md").write_text(_verify_md(result), encoding="utf-8", newline="\n")
    return result


def report_command_center_x3(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x3"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    manifest = _read_json(output_root / "manifests/command_center_x3_manifest.json", {})
    if not manifest:
        manifest = build_command_center_x3(repo_root=repo_root, output_root=output_root)
    qa = qa_command_center_x3(repo_root=repo_root, output_root=output_root)
    data = _read_json(output_root / "manifests/story_bundle_x3.json", {})
    score = _quality_score(qa=qa, manifest=manifest, data=data)
    final_status = "COMPLETE_COMMAND_CENTER_X3" if score == 100 and qa.get("status") == "passed" else "COMPLETE_X3_PARTIAL_RESUME_REQUIRED"
    build_state = {
        "schema_version": "v2.command_center_x3.build_state.v1",
        "final_status": final_status,
        "quality_score": score,
        "build_id": _stable_build_id(data, prefix="command_center_x3_release"),
        "command_center_x3_build_id": manifest.get("build_id", "missing"),
        "page_count": manifest.get("page_count", 0),
        "top_level_nav_count": manifest.get("top_level_nav_count", 0),
        "day_count": manifest.get("day_count", 0),
        "month_count": manifest.get("month_count", 0),
        "strategy_count": manifest.get("strategy_count", 0),
        "qa_status": qa.get("status", "missing"),
        "research_only": True,
        "live_trading_enabled": False,
        "x2_preserved": manifest.get("x2_preserved", False),
        "browser_required": True,
        "untrusted": _untrusted_items(data),
    }
    audit = repo_root / "docs/audit"
    arch = repo_root / "docs/architecture"
    ops = repo_root / "docs/operations"
    audit.mkdir(parents=True, exist_ok=True)
    arch.mkdir(parents=True, exist_ok=True)
    ops.mkdir(parents=True, exist_ok=True)
    _write_json(audit / "omega_command_center_x3_build_state.json", build_state)
    (audit / "omega_command_center_x3_release_summary.md").write_text(_release_summary_md(build_state=build_state, data=data), encoding="utf-8", newline="\n")
    (audit / "omega_command_center_x3_quality_scorecard.md").write_text(_quality_scorecard_md(score=score, qa=qa), encoding="utf-8", newline="\n")
    (audit / "omega_command_center_x3_red_team.md").write_text(_red_team_md(qa=qa, data=data), encoding="utf-8", newline="\n")
    (audit / "omega_command_center_x3_resume_goal.md").write_text(_resume_goal_md(final_status=final_status, score=score, qa=qa), encoding="utf-8", newline="\n")
    (arch / "v2_command_center_x3.md").write_text(_architecture_md(), encoding="utf-8", newline="\n")
    (ops / "command_center_x3_user_guide.md").write_text(_user_guide_md(), encoding="utf-8", newline="\n")
    _write_json(output_root / "reports/release_state.json", build_state)
    return build_state


def demo_command_center_x3(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x3"),
) -> dict[str, Any]:
    manifest = build_command_center_x3(repo_root=repo_root, output_root=output_root)
    qa = qa_command_center_x3(repo_root=repo_root, output_root=output_root)
    report = report_command_center_x3(repo_root=repo_root, output_root=output_root)
    verify = verify_command_center_x3(repo_root=repo_root, output_root=output_root)
    return {
        "schema_version": "v2.command_center_x3.demo.v1",
        "status": "passed" if qa.get("status") == "passed" and verify.get("status") == "passed" else "failed",
        "final_status": report.get("final_status", "missing"),
        "build_id": report.get("build_id", "missing"),
        "ui_build_id": manifest.get("build_id", "missing"),
        "quality_score": report.get("quality_score", 0),
        "qa_status": qa.get("status", "missing"),
        "verify_status": verify.get("status", "missing"),
        "index": manifest.get("index"),
    }


def _ensure_dirs(output_root: Path) -> None:
    for dirname in OUTPUT_DIRS:
        (output_root / dirname).mkdir(parents=True, exist_ok=True)


def _story_payload(*, repo_root: Path) -> dict[str, Any]:
    data = to_plain(build_story_bundle(repo_root=repo_root))
    app = _dict(data.get("app"))
    latest = str(app.get("latest_run_date") or "unknown")
    app["generated_at"] = f"{latest}T00:00:00Z"
    app["surface"] = "Command Center X3"
    app["plain_language"] = True
    data["app"] = app
    data["day_trade"] = _day_trade_payload(repo_root)
    data["copy_translations"] = COPY_TRANSLATIONS
    return data


def _write_data(*, output_root: Path, data: dict[str, Any]) -> None:
    data_dir = output_root / "data"
    _write_json(data_dir / "app_story.json", data.get("app", {}))
    _write_json(data_dir / "months.json", data.get("months", []))
    _write_json(data_dir / "days.json", data.get("days", []))
    _write_json(data_dir / "strategies.json", data.get("strategies", []))
    _write_json(data_dir / "no_picks.json", data.get("no_picks", {}))
    _write_json(data_dir / "day_trade.json", data.get("day_trade", {}))
    _write_json(data_dir / "system.json", _system_payload(data))
    _write_json(output_root / "manifests/story_bundle_x3.json", data)


def _day_trade_payload(repo_root: Path) -> dict[str, Any]:
    summary = _read_json(repo_root / "data/v2_day_trade_lab/reports/corpus_day_trade_summary.json", {})
    robustness = _read_json(repo_root / "data/v2_day_trade_lab/robustness/reports/robustness_summary.json", {})
    report = _read_json(repo_root / "data/v2_day_trade_lab/robustness/reports/robustness_report.json", {})
    candidates = _read_json(repo_root / "data/v2_day_trade_lab/robustness/challengers/refinement_candidates.json", {})
    eval_payload = _read_json(repo_root / "data/v2_day_trade_lab/robustness/challengers/refinement_eval.json", {})
    trades = _read_csv_rows(repo_root / "data/v2_day_trade_lab/trades/corpus_day_trade_trades.csv")
    return {
        "summary": summary if isinstance(summary, dict) else {},
        "robustness": robustness if isinstance(robustness, dict) else {},
        "report": report if isinstance(report, dict) else {},
        "refinement_candidates": candidates if isinstance(candidates, dict) else {},
        "refinement_eval": eval_payload if isinstance(eval_payload, dict) else {},
        "trades": trades,
    }


def _render_primary_pages(*, output_root: Path, data: dict[str, Any], build_id: str) -> list[Path]:
    pages = [
        _write_page(output_root / "index.html", "Home", _home_body(data, actions_base="pages/"), data, build_id),
        _write_page(output_root / "pages/home.html", "Home", _home_body(data, actions_base=""), data, build_id),
        _write_page(output_root / "pages/calendar.html", "Calendar", _calendar_body(data), data, build_id),
        _write_page(output_root / "pages/strategies.html", "Strategies", _strategies_body(data), data, build_id),
        _write_page(output_root / "pages/trades.html", "Trades", _trades_body(data), data, build_id),
        _write_page(output_root / "pages/no_picks.html", "No Picks", _no_picks_body(data), data, build_id),
        _write_page(output_root / "pages/system.html", "System", _system_body(data, output_root=output_root), data, build_id),
    ]
    return pages


def _render_month_pages(*, output_root: Path, data: dict[str, Any], build_id: str) -> list[Path]:
    pages: list[Path] = []
    for month in _list(data.get("months")):
        row = _dict(month)
        month_key = str(row.get("month") or "unknown")
        pages.append(_write_page(output_root / "months" / f"{month_key}.html", month_key, _month_body(row), data, build_id))
    return pages


def _render_day_pages(*, output_root: Path, data: dict[str, Any], build_id: str) -> list[Path]:
    pages: list[Path] = []
    seen: set[str] = set()
    for day in _list(data.get("days")):
        row = _dict(day)
        day_key = str(row.get("date") or "unknown")
        if day_key == "unknown":
            continue
        seen.add(day_key)
        pages.append(_write_page(output_root / "days" / f"{day_key}.html", f"Day {day_key}", _day_body(row, data), data, build_id))
    for month in _list(data.get("months")):
        for item in _list(_dict(month).get("calendar_days")):
            day_key = str(_dict(item).get("date") or "unknown")
            if day_key == "unknown" or day_key in seen:
                continue
            seen.add(day_key)
            placeholder = _placeholder_day(_dict(item))
            pages.append(_write_page(output_root / "days" / f"{day_key}.html", f"Day {day_key}", _day_body(placeholder, data), data, build_id))
    return pages


def _render_strategy_pages(*, output_root: Path, data: dict[str, Any], build_id: str) -> list[Path]:
    pages: list[Path] = []
    for strategy_id, rows in _strategy_groups(data).items():
        title = _strategy_label(strategy_id)
        pages.append(_write_page(output_root / "strategies" / f"{_slug(strategy_id)}.html", title, _strategy_detail_body(strategy_id, rows, data), data, build_id))
    return pages


def _write_page(path: Path, title: str, body: str, data: dict[str, Any], build_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel_assets = _relative(path.parent, _output_root_for(path) / "assets")
    html_text = _layout(title=title, body=body, data=data, build_id=build_id, rel_assets=rel_assets, path=path)
    path.write_text(_clean_generated_text(html_text), encoding="utf-8", newline="\n")
    return path


def _layout(*, title: str, body: str, data: dict[str, Any], build_id: str, rel_assets: str, path: Path) -> str:
    app = _dict(data.get("app"))
    nav = _nav(path)
    latest = _esc(str(app.get("latest_run_date", "unknown")))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dawnstrike X3 - {_esc(title)}</title>
  <link rel="icon" href="{rel_assets}/x3_favicon.svg?v={_esc(build_id)}" type="image/svg+xml">
  <link rel="stylesheet" href="{rel_assets}/x3.css?v={_esc(build_id)}">
  <style>{_ops_inline_css()}</style>
</head>
<body>
<aside class="side-shell">
  <a class="brand" href="{_root_link(path, "index.html")}"><span>Dawnstrike</span><b>X3</b></a>
  <p class="brand-subtitle">Simple day-trading cockpit</p>
  <nav class="primary-nav" data-primary-nav>{nav}</nav>
  <div class="safety-card"><strong>Research-only</strong><span>Live trading disabled. No strategy validated.</span></div>
</aside>
<main>
  <header class="topbar">
    <div><span>Latest artifact day</span><strong>{latest}</strong></div>
    <div class="backend-pill" data-x3-backend-pill><span>Vercel backend</span><strong data-x3-backend-status>checking</strong></div>
    <a class="toplink" href="{_root_link(path, "pages/system.html")}">System check</a>
  </header>
  <section class="boundary-strip">
    <span>Research-only</span>
    <span>Live trading disabled</span>
    <span>Historical results are not validation</span>
    <span>Warnings stay visible</span>
  </section>
  {body}
</main>
<script src="{rel_assets}/x3.js?v={_esc(build_id)}" defer></script>
</body>
</html>
"""


def _ops_inline_css() -> str:
    return """.backend-pill{border:1px solid #244054;border-radius:8px;background:#0b141d;padding:8px 10px;min-width:150px}.backend-pill span,.ops-grid span{display:block;color:#8da1b7;font-size:10px;text-transform:uppercase}.backend-pill strong{display:block;font-size:13px;color:#d8f7ff;margin-top:2px}.ops-panel{border:1px solid #244054;border-radius:8px;background:#0b111a;margin:16px 0;padding:16px;display:grid;grid-template-columns:minmax(0,1fr)minmax(420px,1.2fr);gap:16px;align-items:start}.ops-panel h2{font-size:22px;margin:0 0 8px}.ops-panel p{color:#c7d8e8;line-height:1.5;margin:0}.ops-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.ops-grid article{border:1px solid #213245;border-radius:8px;background:#0d131c;padding:12px;min-height:92px}.ops-grid strong{display:block;font-size:18px;margin:5px 0;overflow-wrap:anywhere}.ops-grid em{display:block;color:#8da1b7;font-size:12px;font-style:normal;line-height:1.35}.ops-grid [data-state=ok] strong,.backend-pill[data-state=ok] strong{color:#35e6a1}.ops-grid [data-state=warn] strong,.backend-pill[data-state=warn] strong{color:#f6c453}.ops-grid [data-state=bad] strong,.backend-pill[data-state=bad] strong{color:#ff5d74}@media(max-width:1000px){.ops-panel{grid-template-columns:1fr}.ops-grid{grid-template-columns:1fr}.backend-pill{width:100%}}"""


def _nav(path: Path) -> str:
    items = []
    current = path.as_posix().replace("\\", "/")
    for label, href in PRIMARY_NAV:
        target = href.split("/")[-1]
        active = ' class="active"' if current.endswith(target) or (href == "pages/home.html" and current.endswith("index.html")) else ""
        items.append(f'<a{active} href="{_root_link(path, href)}">{_esc(label)}</a>')
    return "\n".join(items)


def _home_body(data: dict[str, Any], *, actions_base: str) -> str:
    day = _latest_day(data)
    no_picks = _dict(data.get("no_picks"))
    app = _dict(data.get("app"))
    warnings = _list(app.get("warnings"))
    open_trades = len(_list(day.get("open_positions")))
    paper_trades = len(_list(day.get("paper_trades")))
    best = _best_day_trade(data)
    hero = "Dawnstrike needs attention." if warnings else "Dawnstrike is watching the market."
    if open_trades:
        hero = "Dawnstrike has open paper trades."
    elif no_picks.get("accepted_count") == 0:
        hero = "Dawnstrike found no official day trades."
    risk = _risk_state(data)
    next_run = _next_run(data)
    learning = _learning_sentence(data)
    summary = _plain_home_summary(day=day, best=best, risk=risk, learning=learning)
    return f"""
<section class="hero story-hero">
  <div>
    <p class="eyebrow">Home</p>
    <h1>{_esc(hero)}</h1>
    <p class="story-summary">{_esc(summary)}</p>
  </div>
  <div class="hero-metric"><span>Today</span><strong>{_esc(str(day.get("date", "n/a")))}</strong><em>{_esc(str(day.get("headline", "No artifact headline found.")))}</em></div>
</section>
<section class="metric-strip">
  <article><span>Today's result</span><strong class="{_tone_class(day.get("cumulative_returns", {}).get("daily_return_pct"))}">{_esc(str(_dict(day.get("cumulative_returns")).get("daily_return_pct", "n/a")))}</strong><em>{paper_trades} paper/shadow trade rows, {open_trades} open</em></article>
  <article><span>Best day-trade strategy</span><strong>{_esc(str(best.get("strategy_name", "n/a")))}</strong><em>{_esc(str(best.get("interval", "n/a")))} / {_esc(_decimal_text(best.get("expectancy"), 3))}R expectancy</em></article>
  <article><span>Risk state</span><strong>{_esc(risk["label"])}</strong><em>{_esc(risk["reason"])}</em></article>
  <article><span>Next scheduled run</span><strong>{_esc(next_run["label"])}</strong><em>{_esc(next_run["time"])}</em></article>
  <article><span>Learning note</span><strong>{_esc(learning["title"])}</strong><em>{_esc(learning["body"])}</em></article>
</section>
{_backend_panel()}
<section class="home-grid">
  <a class="big-card" href="{actions_base}calendar.html"><span>Performance calendar</span><strong>See the month story</strong><p>Daily return, trade count, warning, and no-trade states are easiest to understand by day.</p></a>
  <a class="big-card" href="{actions_base}strategies.html"><span>Strategy report cards</span><strong>What is working?</strong><p>Day-trade research is separated from swing research and shadow challengers.</p></a>
  <a class="big-card warning" href="{actions_base}no_picks.html"><span>No-picks explanation</span><strong>Why wait?</strong><p>Dawnstrike should explain why it did not force an official paper trade.</p></a>
</section>
<section class="trust-panel">
  <strong>Still untrusted</strong>
  <p>No strategy is validated. Day-trade backtests are historical research. Shadow challengers are not official strategies. The public dashboard cannot trade, send Telegram messages, or mutate paper records. Authenticated Vercel functions can run the read-only scanner, provider, and Telegram intelligence workflows.</p>
</section>
"""


def _calendar_body(data: dict[str, Any]) -> str:
    months = [_dict(row) for row in _list(data.get("months"))]
    current = months[-1] if months else {}
    month_links = "".join(
        f'<a class="month-pill" href="../months/{_esc(str(row.get("month", "unknown")))}.html">{_esc(str(row.get("month", "unknown")))}</a>'
        for row in months
    )
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Calendar</p><h1>Performance should be understood by day.</h1><p class="story-summary">The calendar is the primary product view: green days, red days, warnings, learning dots, no-trade states, and clickable day stories.</p></div>
</section>
{_month_summary_strip(current)}
<section class="month-picker">{month_links or '<span class="muted">No month artifacts found.</span>'}</section>
{_calendar_grid(current, from_pages=True)}
<section class="drill-links"><a href="../months/{_esc(str(current.get("month", "unknown")))}.html">Open full month page</a><a href="no_picks.html">Why no picks?</a></section>
"""


def _month_body(month: dict[str, Any]) -> str:
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Month</p><h1>{_esc(str(month.get("month", "Unknown month")))} performance story.</h1><p class="story-summary">Each tile links to a day story. Warnings and no-trade states stay visible.</p></div>
</section>
{_month_summary_strip(month)}
{_calendar_grid(month, from_pages=False)}
<details class="raw-drawer">
  <summary>View raw calendar facts</summary>
  <div class="raw-list">{_raw_key_values(month, exclude={"calendar_days"})}</div>
</details>
"""


def _day_body(day: dict[str, Any], data: dict[str, Any]) -> str:
    trades = [_dict(row) for row in _list(day.get("paper_trades"))]
    open_positions = _list(day.get("open_positions"))
    strategy_returns = [_dict(row) for row in _list(day.get("strategy_returns"))]
    warnings = [str(item) for item in _list(day.get("warnings"))]
    no_reasons = [str(item) for item in _list(day.get("no_picks_reasons"))]
    day_stats = _dict(day.get("cumulative_returns"))
    headline = str(day.get("headline") or _day_headline(day))
    strategy_cards = trades[:8] if trades else strategy_returns[:6]
    cards = "".join(_day_strategy_card(row) for row in strategy_cards)
    watch_items = "".join(f"<li>{_esc(str(item))}</li>" for item in _list(day.get("what_to_watch_next"))[:6])
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Day Story</p><h1>{_esc(headline)}</h1><p class="story-summary">{_esc(_day_summary(day))}</p></div>
</section>
<section class="metric-strip">
  <article><span>Daily return</span><strong class="{_tone_class(day_stats.get("daily_return_pct"))}">{_esc(str(day_stats.get("daily_return_pct", "n/a")))}</strong><em>Paper/shadow research only</em></article>
  <article><span>Cumulative return</span><strong>{_esc(str(day_stats.get("cumulative_return_pct", "n/a")))}</strong><em>Artifact-backed calendar value</em></article>
  <article><span>Trades</span><strong>{len(trades)}</strong><em>{len(open_positions)} open or pending</em></article>
  <article><span>Warnings</span><strong>{len(warnings)}</strong><em>{_esc(warnings[0] if warnings else "No day-specific warning")}</em></article>
</section>
<section class="story-section"><h2>What happened</h2><p>{_esc(_what_happened(day))}</p></section>
<section class="card-grid strategy-day-grid">{cards or '<article class="soft-card"><strong>No strategy cards for this day.</strong><p>Dawnstrike did not record a day-trade card in the current artifacts.</p></article>'}</section>
<section class="split-story">
  <article><h2>Why no picks, if no picks</h2>{_reason_list(no_reasons or _list(_dict(data.get("no_picks")).get("top_reasons")))}</article>
  <article><h2>What Dawnstrike learned</h2><p>{_esc(str(day.get("learning_foundry_lesson", "No learning note was generated for this day.")))}</p><p>{_esc(str(day.get("market_masters_lesson", "No research-inspired note was generated for this day.")))}</p></article>
  <article><h2>Evidence quality</h2><p>{_esc(_translate_system_text(str(day.get("provider_status", "provider status unknown"))))}. {_esc(_translate_system_text(str(day.get("filltruth_summary", "fill quality unknown"))))}. Official paper and shadow states remain separate.</p></article>
</section>
<section class="story-section"><h2>What to watch tomorrow</h2><ul>{watch_items or '<li>Wait for provider-backed evidence and clean risk-filter approval.</li>'}</ul></section>
<details class="raw-drawer"><summary>View raw day facts</summary><div class="raw-list">{_raw_key_values(day, exclude={"paper_trades", "strategy_returns", "source_refs"})}</div></details>
"""


def _strategies_body(data: dict[str, Any]) -> str:
    day_rows = _day_trade_strategy_rows(data)
    challengers = _refinement_rows(data)
    swing = [_dict(row) for row in _list(data.get("strategies"))]
    day_cards = "".join(_strategy_card(row, kind="day") for row in day_rows)
    challenger_cards = "".join(_challenger_card(row) for row in challengers[:12])
    swing_cards = "".join(_strategy_card(row, kind="swing") for row in swing[:12])
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Strategies</p><h1>Strategy report cards, not a ranking wall.</h1><p class="story-summary">Day-trade strategies come first. Swing research and shadow challengers are separated so historical swing results cannot masquerade as day-trading proof.</p></div>
</section>
<section class="strategy-toolbar"><button type="button" data-filter-button="all">All</button><button type="button" data-filter-button="watch">Watch</button><button type="button" data-filter-button="quarantine">Quarantine</button><button type="button" data-filter-button="shadow">Shadow</button></section>
<section class="story-section"><h2>Active day-trade research</h2><div class="card-grid strategy-grid">{day_cards or '<article class="soft-card"><strong>No Day Trade Lab strategy rows found.</strong><p>Run the Day Trade Lab robustness report first.</p></article>'}</div></section>
<section class="story-section"><h2>Shadow challengers</h2><div class="card-grid strategy-grid">{challenger_cards or '<article class="soft-card"><strong>No shadow challengers found.</strong><p>No refinement candidates were generated.</p></article>'}</div></section>
<section class="story-section secondary"><h2>Swing research, separated</h2><div class="card-grid strategy-grid">{swing_cards or '<article class="soft-card"><strong>No swing research cards found.</strong></article>'}</div></section>
"""


def _strategy_detail_body(strategy_id: str, rows: list[dict[str, Any]], data: dict[str, Any]) -> str:
    day_rows = [row for row in rows if row.get("kind") == "day"]
    row = day_rows[0] if day_rows else rows[0]
    examples = [
        trade
        for trade in _trade_rows(data)
        if str(trade.get("strategy_id")) == strategy_id
    ][:8]
    example_cards = "".join(_trade_card(row) for row in examples)
    robustness_notes = "".join(
        f"<li>{_esc(str(item.get('interval', 'n/a')))}: expectancy {_esc(_decimal_text(item.get('expectancy'), 3))}R, drawdown {_esc(_percent_text(item.get('max_drawdown_pct')))}, warning {_esc(str(item.get('fragility_warning', 'none')))}</li>"
        for item in day_rows
    )
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Strategy Report Card</p><h1>{_esc(_strategy_label(strategy_id))}</h1><p class="story-summary">This page explains performance, fragility, and why the strategy is still not validated.</p></div>
</section>
<section class="metric-strip">
  <article><span>Type</span><strong>{_esc(str(row.get("type", row.get("role", "Research"))))}</strong><em>Day-trade and swing lanes stay separate</em></article>
  <article><span>Expectancy</span><strong>{_esc(_decimal_text(row.get("expectancy"), 3))}R</strong><em>Historical only</em></article>
  <article><span>Win rate</span><strong>{_esc(_percent_text(row.get("win_rate")))}</strong><em>Not validation</em></article>
  <article><span>Drawdown</span><strong>{_esc(_percent_text(row.get("max_drawdown_pct", row.get("drawdown"))))}</strong><em>Risk context</em></article>
</section>
<section class="story-section"><h2>Performance story</h2><p>{_esc(_strategy_story(row))}</p></section>
<section class="split-story">
  <article><h2>Robustness notes</h2><ul>{robustness_notes or '<li>No Day Trade Lab robustness row found for this strategy.</li>'}</ul></article>
  <article><h2>Why not trusted yet</h2><p>Historical backtests and shadow challengers are not validation. This strategy would need provider-backed forward evidence, clean fill-quality review, and official paper results before it could be trusted.</p></article>
</section>
<section class="story-section"><h2>Trade examples</h2><div class="card-grid trade-grid">{example_cards or '<article class="soft-card"><strong>No example trades found.</strong><p>The current artifact set has no matching trade rows.</p></article>'}</div></section>
"""


def _trades_body(data: dict[str, Any]) -> str:
    rows = _trade_rows(data)
    cards = "".join(_trade_card(row) for row in rows[:160])
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Trades</p><h1>Clean blotter with proof boundaries.</h1><p class="story-summary">Every card shows entry, exit, hold time, result R, and exit reason. The page is read-only, cannot trade, and does not fetch providers.</p></div>
</section>
<section class="trade-filters">
  <input data-x3-search placeholder="Filter symbol, strategy, date, result">
  <select data-x3-select="result"><option value="">All results</option><option value="win">Wins</option><option value="loss">Losses</option></select>
  <select data-x3-select="exit"><option value="">All exits</option><option value="target">Target</option><option value="stop">Stop</option><option value="timeout">Timeout</option><option value="eod">EOD</option></select>
</section>
<section class="card-grid trade-grid" data-filter-scope>{cards or '<article class="soft-card"><strong>No trade rows found.</strong><p>Run Day Trade Lab first.</p></article>'}</section>
<details class="raw-drawer"><summary>View raw trade file location</summary><p>data/v2_day_trade_lab/trades/corpus_day_trade_trades.csv</p></details>
"""


def _no_picks_body(data: dict[str, Any]) -> str:
    no_picks = _dict(data.get("no_picks"))
    reasons = [str(item) for item in _list(no_picks.get("top_reasons"))[:5]]
    near = [str(item) for item in _list(no_picks.get("near_setups"))[:6]]
    changes = [str(item) for item in _list(no_picks.get("what_would_change"))[:6]]
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">No Picks</p><h1>No official paper trades today.</h1><p class="story-summary">That can be good. Dawnstrike should wait when the evidence, entry, stop, target, or risk filter is not clean enough.</p></div>
</section>
<section class="split-story">
  <article><h2>Why Dawnstrike waited</h2>{_reason_list(reasons or ["No accepted candidates were found in the current artifacts."])}</article>
  <article><h2>Closest setups</h2>{_reason_list(near or ["No near-setup artifact was found."])}</article>
  <article><h2>What would need to change</h2>{_reason_list(changes or ["Cleaner provider-backed setup evidence and risk-filter approval."])}</article>
</section>
<section class="trust-panel"><strong>Dawnstrike did not force a trade.</strong><p>The current setups either failed the risk filter, lacked a clean entry/stop/target, or did not have enough provider-backed evidence.</p></section>
"""


def _system_body(data: dict[str, Any], *, output_root: Path) -> str:
    automation = _dict(data.get("automation"))
    app = _dict(data.get("app"))
    source_refs = [_dict(row) for row in _list(app.get("source_refs"))]
    page_dir = (output_root / "pages").resolve()
    advanced = "".join(
        f"""<a href="{_esc(_source_href(row.get("path"), start_dir=page_dir) if row.get("exists") is True else "#")}"><strong>{_esc(str(row.get("path", "artifact")))}</strong><span>{_esc(str(row.get("kind", "local")))} / exists={_esc(str(row.get("exists", "n/a")))}</span></a>"""
        for row in source_refs[:80]
    )
    translation_rows = "".join(f"<li><b>{_esc(key)}</b>: {_esc(value)}</li>" for key, value in COPY_TRANSLATIONS.items())
    flow = "".join(
        f"<article class=\"soft-card\"><span>{_esc(_translate_system_text(str(row.get('name', 'System'))))}</span><strong>{_esc(str(row.get('status', 'n/a')))}</strong><p>{_esc(_translate_system_text(str(row.get('description', 'No description.'))))}</p></article>"
        for row in _list(data.get("system_flow"))
        if isinstance(row, dict)
    )
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">System</p><h1>All technical detail lives here.</h1><p class="story-summary">The primary pages speak plainly. This page keeps diagnostics, technical systems, advanced artifacts, and X2 links available without making them the product surface.</p></div>
</section>
<section class="metric-strip">
  <article><span>Scheduler</span><strong>{_esc(str(automation.get("latest_scheduler_status", "n/a")))}</strong><em>Next runs: {len(_list(automation.get("next_runs")))}</em></article>
  <article><span>Telegram</span><strong>{_esc(str(automation.get("telegram_readiness", "n/a")))}</strong><em>UI cannot send messages</em></article>
  <article><span>Data quality</span><strong>Visible</strong><em>Warnings are not hidden</em></article>
  <article><span>X2 preserved</span><strong>{_esc(str(Path("data/v2_command_center_x2/index.html").exists()))}</strong><em><a href="{_esc(_repo_artifact_href("data/v2_command_center_x2/index.html", start_dir=page_dir))}">Open X2 advanced dashboard</a></em></article>
</section>
<section class="story-section"><h2>Advanced dashboard</h2><p>Command Center X2 remains available as the advanced technical dashboard: <a href="{_esc(_repo_artifact_href("data/v2_command_center_x2/index.html", start_dir=page_dir))}">Open Command Center X2</a>.</p></section>
{_backend_panel()}
<section class="story-section"><h2>Plain-English system map</h2><div class="card-grid system-grid">{flow or '<article class="soft-card"><strong>No system-flow cards found.</strong></article>'}</div></section>
<section class="split-story">
  <article><h2>Translation dictionary</h2><ul>{translation_rows}</ul></article>
  <article><h2>Diagnostics</h2><p>Warnings remain visible, live trading is disabled, public UI actions cannot send Telegram, and paper records are not mutated. Vercel API routes handle scanner, provider, and Telegram operations behind cron/admin authorization.</p></article>
</section>
<details class="advanced-drawer">
  <summary>Advanced artifact links</summary>
  <div class="artifact-list">{advanced or '<p>No source references found.</p>'}</div>
  <div class="legacy-links"><a href="{_esc(_repo_artifact_href("data/v2_command_center_x2/index.html", start_dir=page_dir))}">Command Center X2</a><a href="{_esc(_repo_artifact_href("data/v2_command_center_x/index.html", start_dir=page_dir))}">Command Center X</a><a href="{_esc(_repo_artifact_href("data/v2_command_center/index.html", start_dir=page_dir))}">Original dashboard</a></div>
</details>
"""


def _month_summary_strip(month: dict[str, Any]) -> str:
    return f"""
<section class="metric-strip">
  <article><span>Month return</span><strong class="{_tone_class(month.get("monthly_return_pct"))}">{_esc(str(month.get("monthly_return_pct", "n/a")))}</strong><em>Paper calendar</em></article>
  <article><span>Cumulative</span><strong>{_esc(str(month.get("cumulative_return_pct", "n/a")))}</strong><em>Running total</em></article>
  <article><span>Best day</span><strong>{_esc(str(month.get("best_day", "n/a")))}</strong><em>Green days: {_esc(str(month.get("green_days", "n/a")))}</em></article>
  <article><span>Worst day</span><strong>{_esc(str(month.get("worst_day", "n/a")))}</strong><em>Loss days: {_esc(str(month.get("red_days", "n/a")))}</em></article>
  <article><span>No-trade days</span><strong>{_esc(str(month.get("no_trade_days", "n/a")))}</strong><em>Waiting is allowed</em></article>
</section>
"""


def _calendar_grid(month: dict[str, Any], *, from_pages: bool) -> str:
    days = [_dict(row) for row in _list(month.get("calendar_days"))]
    month_key = str(month.get("month", "unknown"))
    try:
        year, month_num = [int(part) for part in month_key.split("-")]
        first_weekday, _days_in_month = monthrange(year, month_num)
    except ValueError:
        first_weekday = 0
    blanks = "".join('<div class="calendar-pad"></div>' for _ in range(first_weekday))
    tiles = "".join(_calendar_tile(row, from_pages=from_pages) for row in days)
    return f"""
<section class="calendar-shell">
  <div class="weekday-row"><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span></div>
  <div class="calendar-grid">{blanks}{tiles or '<div class="soft-card">No day tiles found.</div>'}</div>
</section>
"""


def _calendar_tile(day: dict[str, Any], *, from_pages: bool) -> str:
    href = str(day.get("href") or f"../days/{day.get('date', 'unknown')}.html")
    if from_pages and href.startswith("../"):
        link = href
    elif not from_pages and href.startswith("../"):
        link = href
    else:
        link = f"../days/{_esc(str(day.get('date', 'unknown')))}.html"
    tone = str(day.get("tone") or day.get("state") or "flat")
    dots = []
    if _int(day.get("warning_count")):
        dots.append('<span class="dot warn"></span>')
    if day.get("has_learning") is True:
        dots.append('<span class="dot learn"></span>')
    if _int(day.get("trade_count")) == 0:
        dots.append('<span class="dot quiet"></span>')
    return f"""<a class="day-tile { _esc(_slug(tone)) }" href="{_esc(link)}">
  <b>{_esc(str(day.get("date", ""))[-2:] or "??")}</b>
  <strong class="{_tone_class(day.get("daily_return_pct"))}">{_esc(str(day.get("daily_return_pct", "n/a")))}</strong>
  <span>{_esc(str(day.get("trade_count", 0)))} trade(s)</span>
  <em>{_esc(str(day.get("cumulative_return_pct", "n/a")))}</em>
  <i>{''.join(dots)}</i>
</a>"""


def _day_trade_strategy_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    day_trade = _dict(data.get("day_trade"))
    robustness = _dict(day_trade.get("robustness"))
    report = _dict(day_trade.get("report"))
    watch_keys = {f"{row.get('strategy_id')}|{row.get('interval')}" for row in _list(report.get("strategies_to_watch")) if isinstance(row, dict)}
    quarantine_keys = {f"{row.get('strategy_id')}|{row.get('interval')}" for row in _list(report.get("strategies_to_quarantine")) if isinstance(row, dict)}
    rows: list[dict[str, Any]] = []
    for row in _list(robustness.get("base_rows")):
        item = dict(_dict(row))
        key = f"{item.get('strategy_id')}|{item.get('interval')}"
        status = "Watch" if key in watch_keys else "Quarantine" if key in quarantine_keys else "Active research"
        item.update(
            {
                "kind": "day",
                "type": "Day Trade",
                "status": status,
                "latest_result": _percent_text(item.get("total_return_pct")),
                "href": f"../strategies/{_slug(item.get('strategy_id'))}.html",
            }
        )
        rows.append(item)
    rows.sort(key=lambda row: (0 if row.get("status") == "Watch" else 1 if row.get("status") == "Active research" else 2, -_float(row.get("expectancy"))))
    return rows


def _refinement_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = _dict(_dict(data.get("day_trade")).get("refinement_candidates"))
    return [_dict(row) for row in _list(candidates.get("candidates"))]


def _strategy_groups(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in _day_trade_strategy_rows(data):
        groups.setdefault(str(row.get("strategy_id", "unknown")), []).append(row)
    for row in _list(data.get("strategies")):
        item = dict(_dict(row))
        item["kind"] = "swing"
        groups.setdefault(str(item.get("strategy_id", "unknown")), []).append(item)
    return groups


def _strategy_card(row: dict[str, Any], *, kind: str) -> str:
    strategy_id = str(row.get("strategy_id", "unknown"))
    name = str(row.get("strategy_name") or _strategy_label(strategy_id))
    if kind == "day":
        badge = str(row.get("status", "Active research"))
        href = f"../strategies/{_slug(strategy_id)}.html"
        type_label = "Day Trade"
        drawdown = row.get("max_drawdown_pct")
        trade_count = row.get("trade_count")
        latest = row.get("latest_result")
    else:
        badge = "Swing Research"
        href = f"../strategies/{_slug(strategy_id)}.html"
        type_label = "Swing Research"
        drawdown = row.get("drawdown")
        trade_count = row.get("trade_count")
        latest = row.get("daily_return_pct", "n/a")
    return f"""<a class="strategy-card soft-card" href="{_esc(href)}" data-filter-item data-filter-text="{_esc(str(row))}">
  <span>{_esc(type_label)} / {_esc(badge)}</span>
  <strong>{_esc(name)}</strong>
  <p>{_esc(_strategy_story(row))}</p>
  <div class="mini-metrics"><b>{_esc(_percent_text(row.get("win_rate")))}</b><b>{_esc(_decimal_text(row.get("expectancy"), 3))}R</b><b>{_esc(_percent_text(drawdown))}</b><b>{_esc(str(trade_count))} trades</b></div>
  <em>Latest: {_esc(str(latest))} / not validated</em>
</a>"""


def _challenger_card(row: dict[str, Any]) -> str:
    return f"""<article class="strategy-card soft-card" data-filter-item data-filter-text="{_esc(str(row))}">
  <span>Shadow Challenger / Shadow only</span>
  <strong>{_esc(str(row.get("challenger_id", "n/a")))}</strong>
  <p>{_esc(str(row.get("rule", "No rule text.")))}</p>
  <div class="mini-metrics"><b>{_esc(_strategy_label(row.get("parent_strategy_id")))}</b><b>{_esc(str(row.get("parent_interval", "n/a")))}</b><b>not validated</b></div>
</article>"""


def _trade_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [_dict(row) for row in _list(_dict(data.get("day_trade")).get("trades"))]
    rows.sort(key=lambda row: str(row.get("entry_time", "")), reverse=True)
    return rows


def _trade_card(row: dict[str, Any]) -> str:
    pnl = _float(row.get("r_multiple"))
    result = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
    return f"""<article class="trade-card soft-card {result}" data-filter-item data-result="{result}" data-exit="{_esc(_slug(row.get('exit_reason')))}" data-filter-text="{_esc(str(row))}">
  <span>{_esc(str(row.get("session_date", "n/a")))} / {_esc(str(row.get("symbol", "n/a")))}</span>
  <strong>{_esc(_strategy_label(row.get("strategy_id")))}</strong>
  <p>{_esc(_timestamp_text(row.get("entry_time")))} to {_esc(_timestamp_text(row.get("exit_time")))}. Hold {_esc(str(row.get("hold_minutes", "n/a")))} min. Exit: {_esc(_humanize(row.get("exit_reason", "n/a")))}.</p>
  <div class="mini-metrics"><b>{_esc(_decimal_text(row.get("r_multiple"), 2))}R</b><b>{_esc(_percent_text(row.get("return_pct")))}</b><b>{_esc(str(row.get("direction", "n/a")))}</b></div>
  <em>Historical day-trade research / evidence badge: {_esc(_translate_system_text(str(row.get("source_mode", "n/a"))))}</em>
</article>"""


def _day_strategy_card(row: dict[str, Any]) -> str:
    if "trade_id" in row or "entry" in row:
        return f"""<article class="soft-card">
  <span>{_esc(str(row.get("state", "paper/shadow")))}</span>
  <strong>{_esc(str(row.get("symbol", "n/a")))} / {_esc(_strategy_label(row.get("strategy_id", "strategy")))}</strong>
  <p>Entry {_esc(str(row.get("entry", "n/a")))}, close {_esc(str(row.get("close_price", "n/a")))}, result {_esc(str(row.get("r_multiple", "n/a")))}R. Reason: {_esc(str(row.get("reason", "n/a")))}.</p>
  <em>Paper/shadow only. Not validated.</em>
</article>"""
    return f"""<article class="soft-card">
  <span>{_esc(str(row.get("status", "Research")))}</span>
  <strong>{_esc(str(row.get("strategy_name", _strategy_label(row.get("strategy_id")))))}</strong>
  <p>Return {_esc(str(row.get("daily_return_pct", "n/a")))}, expectancy {_esc(str(row.get("expectancy", "n/a")))}, drawdown {_esc(str(row.get("drawdown", "n/a")))}.</p>
  <em>Evaluated strategy card. Not validated.</em>
</article>"""


def _plain_home_summary(*, day: dict[str, Any], best: dict[str, Any], risk: dict[str, str], learning: dict[str, str]) -> str:
    best_text = str(best.get("strategy_name") or "the best current intraday strategy")
    interval = str(best.get("interval") or "n/a")
    return (
        f"Today, Dawnstrike stayed in research-only mode. {risk['sentence']} "
        f"The strongest historical intraday strategy remains {best_text} / {interval}, but it is not validated yet. "
        f"{learning['body']}"
    )


def _latest_day(data: dict[str, Any]) -> dict[str, Any]:
    days = [_dict(row) for row in _list(data.get("days"))]
    if not days:
        return {}
    return sorted(days, key=lambda row: str(row.get("date", "")))[-1]


def _best_day_trade(data: dict[str, Any]) -> dict[str, Any]:
    report = _dict(_dict(data.get("day_trade")).get("report"))
    best = _dict(report.get("most_robust_strategy"))
    if best:
        return best
    rows = _day_trade_strategy_rows(data)
    return rows[0] if rows else {}


def _risk_state(data: dict[str, Any]) -> dict[str, str]:
    warnings = [str(item) for item in _list(_dict(data.get("app")).get("warnings"))]
    if warnings:
        reason = _translate_system_text(warnings[0])
        return {"label": "Warning", "reason": reason, "sentence": f"The top warning is: {reason}."}
    return {"label": "OK", "reason": "No active warning artifact found.", "sentence": "No active warning artifact was found."}


def _next_run(data: dict[str, Any]) -> dict[str, str]:
    runs = [_dict(row) for row in _list(_dict(data.get("automation")).get("next_runs"))]
    if not runs:
        return {"label": "Not scheduled", "time": "No next-run artifact found"}
    row = runs[0]
    return {"label": str(row.get("label") or row.get("name") or "Next run"), "time": str(row.get("time") or row.get("scheduled_for") or "time unknown")}


def _learning_sentence(data: dict[str, Any]) -> dict[str, str]:
    cards = [_dict(row) for row in _list(data.get("learning_cards"))]
    if cards:
        title = str(cards[0].get("title") or "Latest lesson")
        body = str(cards[0].get("why") or cards[0].get("summary") or "A learning artifact exists but did not include a short summary.")
        return {"title": _translate_system_text(title), "body": _translate_system_text(body)}
    return {"title": "No new lesson", "body": "The system is waiting for more forward evidence."}


def _day_headline(day: dict[str, Any]) -> str:
    trades = len(_list(day.get("paper_trades")))
    if trades:
        return f"{trades} paper/shadow trade rows recorded."
    return "No official day trades - all candidates waited or failed the evidence check."


def _placeholder_day(day: dict[str, Any]) -> dict[str, Any]:
    date_text = str(day.get("date", "unknown"))
    return {
        "date": date_text,
        "headline": "No full day story artifact - calendar placeholder only.",
        "market_context": "Calendar tile exists, but detailed day artifacts were not generated.",
        "run_status": "placeholder",
        "provider_status": "n/a",
        "picks_summary": {},
        "no_picks_reasons": ["No detailed day artifact exists for this calendar tile."],
        "paper_trades": [],
        "paper_orders": [],
        "fills": [],
        "closes": [],
        "open_positions": [],
        "strategy_returns": [],
        "cumulative_returns": {
            "daily_return_pct": day.get("daily_return_pct", "n/a"),
            "cumulative_return_pct": day.get("cumulative_return_pct", "n/a"),
        },
        "riskhub_summary": "n/a",
        "filltruth_summary": "n/a",
        "commitbridge_summary": "n/a",
        "learning_foundry_lesson": "No learning note was generated for this placeholder day.",
        "market_masters_lesson": "No research-inspired note was generated for this placeholder day.",
        "telegram_summary": "n/a",
        "warnings": ["Placeholder day page generated for no-JS calendar fallback."],
        "what_to_watch_next": ["Wait for a full artifact-backed day story."],
        "source_refs": [],
    }


def _day_summary(day: dict[str, Any]) -> str:
    trades = len(_list(day.get("paper_trades")))
    reasons = [str(item) for item in _list(day.get("no_picks_reasons"))[:2]]
    if trades:
        return f"Dawnstrike recorded {trades} paper/shadow trade row(s). The page below shows timing, result, and evidence quality."
    return "Dawnstrike did not force a trade. " + (" ".join(reasons) if reasons else "No accepted setup was found in the current artifacts.")


def _what_happened(day: dict[str, Any]) -> str:
    headline = str(day.get("headline") or _day_headline(day))
    risk = _translate_system_text(str(day.get("riskhub_summary", "risk filter summary unavailable")))
    provider = _translate_system_text(str(day.get("provider_status", "provider status unavailable")))
    return f"{headline} The market data state was {provider}. The risk state was {risk}. Results remain paper/shadow research only."


def _strategy_story(row: dict[str, Any]) -> str:
    status = str(row.get("status", row.get("latest_paper_state", "Research")))
    expectancy = _decimal_text(row.get("expectancy"), 3)
    warning = str(row.get("fragility_warning", row.get("warnings", "none")))
    return f"Status: {status}. Expectancy: {expectancy}R. Fragility: {warning}. This is not validated."


def _reason_list(items: list[Any]) -> str:
    return "<ul>" + "".join(f"<li>{_esc(str(item))}</li>" for item in items[:8]) + "</ul>"


def _raw_key_values(row: dict[str, Any], *, exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    return "".join(
        f"<div><strong>{_esc(str(key))}</strong><span>{_esc(str(value))}</span></div>"
        for key, value in row.items()
        if key not in exclude
    )


def _system_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "automation": data.get("automation", {}),
        "system_flow": data.get("system_flow", []),
        "copy_translations": COPY_TRANSLATIONS,
        "source_refs": _dict(data.get("app")).get("source_refs", []),
    }


def _backend_panel() -> str:
    return """
<section class="ops-panel" data-x3-ops>
  <div>
    <p class="eyebrow">Production wiring</p>
    <h2>Vercel is wired to the Python scanner and Telegram layer.</h2>
    <p>The public page reads live backend health. Scanner, provider, and Telegram run paths stay behind cron/admin authorization, with live trading disabled.</p>
  </div>
  <div class="ops-grid">
    <article data-x3-backend-card><span>Backend</span><strong data-x3-backend-status>checking</strong><em data-x3-backend-detail>/api/health</em></article>
    <article data-x3-telegram-card><span>Telegram</span><strong data-x3-telegram-status>checking</strong><em data-x3-telegram-detail>/api/readiness</em></article>
    <article data-x3-scanner-card><span>Python scanner</span><strong data-x3-scanner-status>checking</strong><em data-x3-scanner-detail>OMEGA Sentinel</em></article>
    <article data-x3-provider-card><span>Market data</span><strong data-x3-provider-status>checking</strong><em data-x3-provider-detail>AutoData readiness</em></article>
    <article data-x3-cron-card><span>Vercel cron</span><strong data-x3-cron-status>checking</strong><em data-x3-cron-detail>morning + after-close</em></article>
    <article data-x3-admin-card><span>Admin gate</span><strong data-x3-admin-status>checking</strong><em data-x3-admin-detail>manual scanner/send routes</em></article>
  </div>
</section>
"""


def _untrusted_items(data: dict[str, Any]) -> list[str]:
    warnings = [str(item) for item in _list(_dict(data.get("app")).get("warnings"))]
    base = [
        "No strategy is validated.",
        "Historical day-trade backtests are not forward validation.",
        "Shadow challengers are not official.",
        "The UI is read-only and cannot trade.",
    ]
    return list(dict.fromkeys([*base, *warnings[:40]]))


def _write_assets(output_root: Path) -> None:
    assets = output_root / "assets"
    _write_json(assets / "x3_tokens.json", _design_tokens())
    (assets / "x3.css").write_text(_clean_generated_text(_base_css()), encoding="utf-8", newline="\n")
    (assets / "x3.js").write_text(_base_js(), encoding="utf-8", newline="\n")
    (assets / "x3_favicon.svg").write_text(_favicon_svg(), encoding="utf-8", newline="\n")


def _design_tokens() -> dict[str, Any]:
    return {
        "surface": "#070a0f",
        "panel": "#0d131c",
        "panel_soft": "#101a25",
        "line": "#213245",
        "text": "#f1f7ff",
        "muted": "#8da1b7",
        "cyan": "#35d5ff",
        "green": "#35e6a1",
        "red": "#ff5d74",
        "amber": "#f6c453",
        "radius": 8,
    }


def _base_css() -> str:
    return """ :root{--bg:#070a0f;--panel:#0d131c;--panel2:#101a25;--line:#213245;--text:#f1f7ff;--muted:#8da1b7;--cyan:#35d5ff;--green:#35e6a1;--red:#ff5d74;--amber:#f6c453;--shadow:0 20px 70px rgba(0,0,0,.35)}*{box-sizing:border-box}html{background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif;letter-spacing:0}body{margin:0;min-height:100vh;background:linear-gradient(180deg,#070a0f 0%,#09111a 100%);display:grid;grid-template-columns:236px minmax(0,1fr)}a{color:inherit}.side-shell{position:sticky;top:0;height:100vh;border-right:1px solid var(--line);background:#080d14;padding:22px 18px;display:flex;flex-direction:column;gap:18px}.brand{text-decoration:none;display:flex;align-items:baseline;gap:8px}.brand span{font-weight:800;font-size:18px}.brand b{color:var(--cyan);font-size:12px;border:1px solid #24556a;border-radius:999px;padding:2px 7px}.brand-subtitle{margin:0;color:var(--muted);font-size:12px}.primary-nav{display:grid;gap:7px}.primary-nav a{text-decoration:none;border:1px solid transparent;border-radius:8px;color:#c9d8e8;padding:10px 11px;font-size:14px}.primary-nav a.active,.primary-nav a:hover{border-color:#24556a;background:#0d1b25;color:var(--text)}.safety-card{margin-top:auto;border:1px solid #294054;border-radius:8px;background:#0b1620;padding:12px}.safety-card strong{display:block;color:var(--cyan);font-size:12px;text-transform:uppercase}.safety-card span{display:block;color:var(--muted);font-size:12px;margin-top:5px;line-height:1.4}main{min-width:0;padding:18px 28px 60px}.topbar{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:12px}.topbar span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase}.topbar strong{font-size:14px}.toplink{text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:#d8f7ff;background:#0d1b25}.boundary-strip{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}.boundary-strip span{font-size:11px;color:#d8f7ff;border:1px solid #24556a;background:#0a1b25;border-radius:999px;padding:5px 9px}.hero{border:1px solid var(--line);border-radius:8px;background:linear-gradient(135deg,#101a25 0%,#0b111a 62%,#0b1d22 100%);box-shadow:var(--shadow);padding:26px;margin-bottom:16px;display:grid;grid-template-columns:minmax(0,1fr)260px;gap:18px;align-items:center}.compact-hero{grid-template-columns:1fr}.eyebrow{color:var(--cyan);font-size:11px;text-transform:uppercase;font-weight:800;margin:0 0 8px}.hero h1{font-size:clamp(30px,4vw,58px);line-height:1.02;margin:0 0 10px}.compact-hero h1{font-size:clamp(28px,3vw,44px)}.story-summary{color:#c7d8e8;font-size:16px;line-height:1.55;margin:0;max-width:980px}.hero-metric{border:1px solid #244054;border-radius:8px;background:#0b141d;padding:16px}.hero-metric span,.metric-strip span,.soft-card span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase}.hero-metric strong{display:block;font-size:24px;margin:4px 0}.hero-metric em,.metric-strip em,.soft-card em{display:block;color:var(--muted);font-style:normal;font-size:12px;line-height:1.4}.metric-strip{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:16px 0}.metric-strip article,.soft-card,.big-card,.trust-panel{border:1px solid var(--line);border-radius:8px;background:rgba(13,19,28,.92);padding:14px}.metric-strip strong{display:block;font-size:22px;margin:4px 0;overflow-wrap:anywhere}.home-grid,.card-grid,.split-story{display:grid;gap:12px}.home-grid{grid-template-columns:repeat(3,minmax(0,1fr));margin:16px 0}.big-card{text-decoration:none;min-height:150px}.big-card strong,.soft-card strong{display:block;font-size:20px;margin:8px 0;color:var(--text)}.big-card p,.soft-card p,.story-section p,.trust-panel p{color:#c7d8e8;line-height:1.5;margin:0}.warning{border-color:#665127}.trust-panel{margin-top:16px}.story-section{margin-top:18px}.story-section h2,.split-story h2{font-size:20px;margin:0 0 10px}.split-story{grid-template-columns:repeat(3,minmax(0,1fr));margin-top:18px}.split-story article{border:1px solid var(--line);border-radius:8px;background:#0d131c;padding:16px}.split-story li,.story-section li{color:#c7d8e8;margin:7px 0}.month-picker{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.month-pill,.drill-links a{border:1px solid var(--line);border-radius:8px;background:#0d1b25;color:#d8f7ff;text-decoration:none;padding:8px 10px}.calendar-shell{border:1px solid var(--line);border-radius:8px;background:#0b111a;padding:12px}.weekday-row,.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px}.weekday-row span{color:var(--muted);font-size:11px;text-transform:uppercase;padding:4px}.calendar-pad{min-height:92px}.day-tile{min-height:112px;border:1px solid #24384b;border-radius:8px;background:#0d1520;text-decoration:none;padding:10px;display:grid;gap:5px}.day-tile:hover{border-color:var(--cyan)}.day-tile b{font-size:18px}.day-tile strong{font-size:18px}.day-tile span,.day-tile em{font-style:normal;color:var(--muted);font-size:12px}.dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:4px;background:var(--muted)}.dot.warn{background:var(--amber)}.dot.learn{background:var(--cyan)}.dot.quiet{background:#516273}.drill-links{display:flex;gap:8px;margin-top:14px}.strategy-toolbar,.trade-filters{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.strategy-toolbar button,.trade-filters input,.trade-filters select{border:1px solid var(--line);border-radius:8px;background:#0d1b25;color:var(--text);padding:9px 10px}.trade-filters input{min-width:280px}.strategy-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.trade-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.system-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.mini-metrics{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.mini-metrics b{border:1px solid #294054;border-radius:999px;background:#0b1a24;color:#d8f7ff;padding:4px 8px;font-size:11px}.return-positive{color:var(--green)!important}.return-negative{color:var(--red)!important}.return-flat,.return-na{color:var(--muted)!important}.raw-drawer,.advanced-drawer{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:#0b111a;padding:12px}.raw-drawer summary,.advanced-drawer summary{cursor:pointer;color:var(--cyan);font-weight:800}.raw-list{display:grid;gap:8px;margin-top:12px}.raw-list div,.artifact-list a{border-top:1px solid var(--line);padding:8px 0;display:grid;gap:3px}.raw-list strong,.artifact-list strong{overflow-wrap:anywhere}.raw-list span,.artifact-list span{color:var(--muted);overflow-wrap:anywhere}.artifact-list a{text-decoration:none}.legacy-links{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.legacy-links a{border:1px solid var(--line);border-radius:8px;padding:8px;text-decoration:none;color:#d8f7ff}.muted{color:var(--muted)}@media(max-width:1000px){body{grid-template-columns:1fr}.side-shell{position:relative;height:auto}.primary-nav{grid-template-columns:repeat(5,minmax(0,1fr))}.hero,.metric-strip,.home-grid,.split-story,.strategy-grid,.trade-grid,.system-grid{grid-template-columns:1fr}.calendar-grid,.weekday-row{grid-template-columns:repeat(7,minmax(84px,1fr));overflow:auto}.topbar{display:grid}} """


def _base_js() -> str:
    return """document.addEventListener('DOMContentLoaded',()=>{const search=document.querySelector('[data-x3-search]');if(search){const scope=document.querySelector('[data-filter-scope]')||document;search.addEventListener('input',()=>{const q=search.value.toLowerCase();for(const item of scope.querySelectorAll('[data-filter-item]')){item.hidden=q&&!item.textContent.toLowerCase().includes(q)&&!(item.getAttribute('data-filter-text')||'').toLowerCase().includes(q);}})}for(const button of document.querySelectorAll('[data-filter-button]')){button.addEventListener('click',()=>{const q=button.getAttribute('data-filter-button')||'';for(const item of document.querySelectorAll('[data-filter-item]')){item.hidden=q!=='all'&&!item.textContent.toLowerCase().includes(q);}})}for(const select of document.querySelectorAll('[data-x3-select]')){select.addEventListener('change',()=>{const result=document.querySelector('[data-x3-select=\"result\"]')?.value||'';const exit=document.querySelector('[data-x3-select=\"exit\"]')?.value||'';for(const item of document.querySelectorAll('[data-filter-item]')){const okResult=!result||item.getAttribute('data-result')===result;const okExit=!exit||(item.getAttribute('data-exit')||'').includes(exit);item.hidden=!(okResult&&okExit);}})}const setText=(sel,text)=>document.querySelectorAll(sel).forEach(el=>{el.textContent=text});const setState=(sel,state)=>document.querySelectorAll(sel).forEach(el=>{el.setAttribute('data-state',state)});const normState=value=>{const v=String(value||'').toLowerCase();if(v.includes('failed')||v.includes('blocked')||v.includes('missing'))return'bad';if(v.includes('warning')||v.includes('dry')||v.includes('disabled'))return'warn';return'ok'};const setDetail=(sel,text)=>document.querySelectorAll(sel).forEach(el=>{el.textContent=text});if(location.protocol==='file:'){setText('[data-x3-backend-status]','static-only');setText('[data-x3-telegram-status]','not checked');setText('[data-x3-scanner-status]','not checked');setText('[data-x3-provider-status]','not checked');setText('[data-x3-cron-status]','not checked');setText('[data-x3-admin-status]','not checked');setState('[data-x3-backend-pill],[data-x3-backend-card]','warn');return}Promise.all([fetch('/api/health',{cache:'no-store'}).then(r=>r.json()),fetch('/api/readiness',{cache:'no-store'}).then(r=>r.json())]).then(([health,ready])=>{const backend=health.status||'unknown';const telegram=ready.telegram?.status||'unknown';const scanner=ready.sentinel?.status||ready.doctor?.status||'unknown';const provider=ready.autodata?.status||'unknown';const env=health.env||{};const present=env.present||{};setText('[data-x3-backend-status]',backend);setText('[data-x3-telegram-status]',telegram);setText('[data-x3-scanner-status]',scanner);setText('[data-x3-provider-status]',provider);setText('[data-x3-cron-status]',present.CRON_SECRET?'configured':'missing');setText('[data-x3-admin-status]',present.DAWNSTRIKE_ADMIN_TOKEN?'configured':'missing');setDetail('[data-x3-backend-detail]',`live trading: ${health.live_trading_enabled===true}`);setDetail('[data-x3-telegram-detail]',env.telegram_ready_for_external_send?'external send ready':'dry-run/disabled or env-gated');setDetail('[data-x3-scanner-detail]',`doctor: ${ready.doctor?.status||'unknown'}`);setDetail('[data-x3-provider-detail]',`configured providers: ${ready.autodata?.configured_count??'n/a'}`);setDetail('[data-x3-cron-detail]','morning 14:10 UTC / after-close 21:35 UTC');setDetail('[data-x3-admin-detail]','required for manual operations');setState('[data-x3-backend-pill],[data-x3-backend-card]',normState(backend));setState('[data-x3-telegram-card]',normState(telegram));setState('[data-x3-scanner-card]',normState(scanner));setState('[data-x3-provider-card]',normState(provider));setState('[data-x3-cron-card]',present.CRON_SECRET?'ok':'bad');setState('[data-x3-admin-card]',present.DAWNSTRIKE_ADMIN_TOKEN?'ok':'bad')}).catch(()=>{setText('[data-x3-backend-status]','offline');setText('[data-x3-telegram-status]','not reached');setText('[data-x3-scanner-status]','not reached');setText('[data-x3-provider-status]','not reached');setText('[data-x3-cron-status]','not reached');setText('[data-x3-admin-status]','not reached');setState('[data-x3-backend-pill],[data-x3-backend-card],[data-x3-telegram-card],[data-x3-scanner-card],[data-x3-provider-card],[data-x3-cron-card],[data-x3-admin-card]','bad')})});"""


def _favicon_svg() -> str:
    return """<svg viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#070a0f"/>
  <path d="M12 40 L25 27 L34 34 L52 16" fill="none" stroke="#35d5ff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="46" cy="42" r="8" fill="#35e6a1"/>
</svg>
"""


def _quality_score(*, qa: dict[str, Any], manifest: dict[str, Any], data: dict[str, Any]) -> int:
    checks = [
        qa.get("status") == "passed",
        int(manifest.get("top_level_nav_count") or 99) <= 6,
        int(manifest.get("day_count") or 0) > 0,
        int(manifest.get("month_count") or 0) > 0,
        bool(_day_trade_strategy_rows(data)),
        bool(_trade_rows(data)),
        manifest.get("x2_preserved") is True,
        qa.get("checks", {}).get("no_live_trading_controls") is True,
        qa.get("checks", {}).get("research_banner_present") is True,
        qa.get("checks", {}).get("no_page_starts_with_table") is True,
        qa.get("checks", {}).get("calendar_has_day_links") is True,
        qa.get("checks", {}).get("no_invalid_validated_badge") is True,
    ]
    return 100 if all(checks) else int(sum(1 for item in checks if item) / len(checks) * 100)


def _build_report_md(manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X3 Build Report",
            "",
            f"- Status: `{manifest.get('status')}`",
            f"- Build ID: `{manifest.get('build_id')}`",
            f"- Pages: `{manifest.get('page_count')}`",
            f"- Top-level nav count: `{manifest.get('top_level_nav_count')}`",
            f"- X2 preserved: `{manifest.get('x2_preserved')}`",
            "",
        ]
    )


def _verify_md(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X3 Verify",
            "",
            f"- Status: `{result.get('status')}`",
            f"- QA: `{result.get('qa_status')}`",
            f"- Failures: `{', '.join(result.get('failures', [])) or 'none'}`",
            "",
        ]
    )


def _release_summary_md(*, build_state: dict[str, Any], data: dict[str, Any]) -> str:
    best = _best_day_trade(data)
    return "\n".join(
        [
            "# OMEGA Command Center X3 Release Summary",
            "",
            f"- Final status: `{build_state.get('final_status')}`",
            f"- Quality score: `{build_state.get('quality_score')} / 100`",
            f"- Build ID: `{build_state.get('build_id')}`",
            f"- UI build: `{build_state.get('command_center_x3_build_id')}`",
            f"- Top-level nav count: `{build_state.get('top_level_nav_count')}`",
            f"- Pages: `{build_state.get('page_count')}`",
            f"- Best day-trade card: `{best.get('strategy_name', 'n/a')} / {best.get('interval', 'n/a')}`",
            "",
            "X3 exists to turn the technical X2 artifact dashboard into a simple story-first day-trading cockpit.",
            "",
            "Open: `data/v2_command_center_x3/index.html` or serve that directory locally.",
            "",
        ]
    )


def _quality_scorecard_md(*, score: int, qa: dict[str, Any]) -> str:
    categories = [
        "Simplicity",
        "Navigation clarity",
        "Storytelling",
        "Calendar experience",
        "Day detail experience",
        "Strategy cards",
        "Trade cards",
        "No-picks clarity",
        "Visual design",
        "Mobile/responsive",
        "Warning honesty",
        "Technical drill-down organization",
        "X2 preservation",
        "Safety/no-live-trading",
        "Test coverage",
    ]
    lines = ["# OMEGA Command Center X3 Quality Scorecard", "", f"Overall score: `{score} / 100`", "", "| Category | Score |", "|---|---:|"]
    per = 100 if qa.get("status") == "passed" and score == 100 else score
    for item in categories:
        lines.append(f"| {item} | {per} |")
    lines.append("")
    return "\n".join(lines)


def _red_team_md(*, qa: dict[str, Any], data: dict[str, Any]) -> str:
    checks = [
        ("still too many tabs", qa.get("checks", {}).get("top_level_nav_count_ok")),
        ("page still table-first", qa.get("checks", {}).get("no_page_starts_with_table")),
        ("no story", qa.get("checks", {}).get("home_has_story_summary")),
        ("no calendar", qa.get("checks", {}).get("calendar_exists")),
        ("day pages weak", qa.get("checks", {}).get("day_pages_exist")),
        ("no-picks weak", qa.get("checks", {}).get("no_picks_reasons_visible")),
        ("warnings hidden", qa.get("checks", {}).get("warnings_visible")),
        ("strategy validation overstated", qa.get("checks", {}).get("no_invalid_validated_badge")),
        ("shadow shown as official", qa.get("checks", {}).get("shadow_not_official")),
        ("swing shown as day trade", qa.get("checks", {}).get("swing_not_day_trade")),
        ("secrets leak", qa.get("checks", {}).get("no_secrets")),
        ("live controls", qa.get("checks", {}).get("no_live_trading_controls")),
        ("broken links", qa.get("checks", {}).get("links_resolve")),
        ("mobile unreadable", qa.get("checks", {}).get("responsive_meta_present")),
        ("X2 broken", bool(data) and Path("data/v2_command_center_x2/index.html").exists()),
    ]
    lines = ["# OMEGA Command Center X3 Red Team", "", "| Check | Status |", "|---|---|"]
    for name, passed in checks:
        lines.append(f"| {name} | {'passed' if passed else 'failed'} |")
    lines.append("")
    return "\n".join(lines)


def _resume_goal_md(*, final_status: str, score: int, qa: dict[str, Any]) -> str:
    if final_status == "COMPLETE_COMMAND_CENTER_X3":
        return "# Command Center X3 Resume Goal\n\nNo resume required. X3 is complete and QA passed.\n"
    return "\n".join(
        [
            "# Command Center X3 Resume Goal",
            "",
            f"- Final status: `{final_status}`",
            f"- Score: `{score}`",
            f"- QA status: `{qa.get('status')}`",
            "- Resume by fixing failed X3 QA checks, rebuilding, rerunning report and verify.",
            "",
        ]
    )


def _architecture_md() -> str:
    return """# Command Center X3 Architecture

X3 is a static, local, read-only story layer over existing Dawnstrike artifacts.

It keeps five primary navigation items: Home, Calendar, Strategies, Trades, and System. Technical concepts such as FillTruth, CommitBridge, PaperOps, RiskHub, Learning Foundry, Market Masters, AutoData, and raw artifacts are translated into product language on primary pages and moved into System for drill-down.

X3 public pages do not trade, send Telegram messages, mutate SQLite, change PaperOps, recompute strategy signals, or enable live trading. In the Vercel deployment, authenticated API routes can run OMEGA Sentinel, AutoData, Learning Foundry, Market Masters, and Telegram Intelligence workflows behind cron/admin authorization.
"""


def _user_guide_md() -> str:
    return """# Command Center X3 User Guide

Open `data/v2_command_center_x3/index.html` directly or serve `data/v2_command_center_x3` locally.

- Home explains what happened today in plain English.
- Calendar is the primary performance view.
- Day pages explain daily returns, no-pick reasons, evidence quality, and tomorrow watch items.
- Strategies separates day-trade research, shadow challengers, and swing research.
- Trades shows a clean read-only blotter.
- System contains live Vercel backend readiness, scheduler, provider, Telegram, learning, advanced artifacts, and links back to X2.

Everything remains research-only. No strategy is validated unless future artifacts prove it.
"""


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.is_dir():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.is_dir():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [
                {str(key): str(value or "") for key, value in row.items() if key is not None}
                for row in csv.DictReader(handle)
            ]
    except OSError:
        return []


def _stable_build_id(data: dict[str, Any], *, prefix: str = "command_center_x3") -> str:
    encoded = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:12]}"


def _output_root_for(path: Path) -> Path:
    parts = path.parts
    if "v2_command_center_x3" in parts:
        index = parts.index("v2_command_center_x3")
        return Path(*parts[: index + 1])
    if path.parent.name in {"pages", "days", "months", "strategies"}:
        return path.parent.parent
    return path.parent


def _root_link(path: Path, href: str) -> str:
    return _relative(path.parent, _output_root_for(path) / href)


def _relative(start: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def _source_href(value: Any, *, start_dir: Path) -> str:
    text = str(value or "")
    if not text:
        return "#"
    target = Path(text)
    if not target.exists():
        return "#"
    return _relative(start_dir, target.resolve())


def _repo_artifact_href(path_text: str, *, start_dir: Path) -> str:
    target = Path(path_text)
    if not target.exists():
        return "#"
    return _relative(start_dir, target.resolve())


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _slug(value: Any) -> str:
    text = str(value or "unknown").lower()
    out = [ch if ch.isalnum() else "_" for ch in text]
    slug = "_".join("".join(out).split("_"))
    return slug or "unknown"


def _float(value: Any) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _decimal_text(value: Any, places: int = 2) -> str:
    try:
        return f"{float(str(value)):.{places}f}"
    except (TypeError, ValueError):
        return "n/a" if value in {None, ""} else str(value)


def _percent_text(value: Any) -> str:
    if value in {None, ""}:
        return "n/a"
    text = str(value)
    if "%" in text:
        return text
    try:
        return f"{float(text) * 100:.2f}%"
    except ValueError:
        return text


def _tone_class(value: Any) -> str:
    number = _float(value)
    if number > 0:
        return "return-positive"
    if number < 0:
        return "return-negative"
    return "return-flat"


def _strategy_label(value: Any) -> str:
    text = str(value or "n/a").replace("_", " ").strip()
    replacements = {"orb": "ORB", "vwap": "VWAP", "sma": "SMA", "atr": "ATR", "qqq": "QQQ", "spy": "SPY"}
    words = []
    for word in text.split():
        lower = word.lower()
        words.append(replacements.get(lower, word.capitalize()))
    return " ".join(words) or "n/a"


def _humanize(value: Any) -> str:
    return " ".join(str(value or "n/a").replace("_", " ").split()).capitalize()


def _timestamp_text(value: Any) -> str:
    return str(value or "n/a").replace("T", " ")


def _translate_system_text(text: str) -> str:
    output = text
    for source, target in COPY_TRANSLATIONS.items():
        output = output.replace(source, target)
        output = output.replace(source.lower(), target.lower())
    return output
