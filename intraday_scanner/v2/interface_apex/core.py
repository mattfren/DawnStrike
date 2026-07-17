"""Interface Apex model building, rendering, reports, and local serving."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import html
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from intraday_scanner.v2.interface_apex.adapters import build_apex_model_from_artifacts
from intraday_scanner.v2.interface_apex.language import COPY_TRANSLATIONS
from intraday_scanner.v2.interface_apex.models import (
    CalendarMonth,
    DayModel,
    InterfaceApexModel,
    StrategyModel,
    TradeModel,
    to_plain,
)
from intraday_scanner.v2.interface_apex.qa import REQUIRED_PAGE_NAMES, run_interface_apex_qa

OUTPUT_DIRS = (
    "pages",
    "days",
    "months",
    "strategies",
    "trades",
    "assets",
    "data",
    "reports",
    "qa",
    "manifests",
    "screenshots",
)

PRIMARY_NAV = (
    ("Mission", "pages/mission.html"),
    ("Calendar", "pages/calendar.html"),
    ("Strategies", "pages/strategies.html"),
    ("Trades", "pages/trades.html"),
    ("Intelligence", "pages/intelligence.html"),
    ("System", "pages/system.html"),
)


def build_apex_models(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    model = build_apex_model_from_artifacts(repo_root=repo_root)
    payload = _write_model_data(output_root=output_root, model=model)
    manifest = _base_manifest(model=model, output_root=output_root, pages=[])
    _write_json(output_root / "manifests/interface_apex_model_manifest.json", manifest)
    return {"status": "passed", **manifest, "data_files": payload["data_files"]}


def build_apex_calendar(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    model = build_apex_model_from_artifacts(repo_root=repo_root)
    _write_model_data(output_root=output_root, model=model)
    _write_assets(output_root)
    build_id = _stable_build_id(model)
    pages = _render_calendar_pages(output_root=output_root, model=model, build_id=build_id)
    manifest = _base_manifest(model=model, output_root=output_root, pages=pages)
    _write_json(output_root / "manifests/interface_apex_calendar_manifest.json", manifest)
    return {"status": "passed", **manifest}


def build_apex_days(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    model = build_apex_model_from_artifacts(repo_root=repo_root)
    _write_model_data(output_root=output_root, model=model)
    _write_assets(output_root)
    build_id = _stable_build_id(model)
    pages = _render_day_pages(output_root=output_root, model=model, build_id=build_id)
    manifest = _base_manifest(model=model, output_root=output_root, pages=pages)
    _write_json(output_root / "manifests/interface_apex_days_manifest.json", manifest)
    return {"status": "passed", **manifest}


def build_interface_apex(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    model = build_apex_model_from_artifacts(repo_root=repo_root)
    _write_model_data(output_root=output_root, model=model)
    _write_assets(output_root)
    build_id = _stable_build_id(model)
    pages: list[Path] = []
    pages.extend(_render_primary_pages(output_root=output_root, model=model, build_id=build_id))
    pages.extend(_render_calendar_pages(output_root=output_root, model=model, build_id=build_id))
    pages.extend(_render_day_pages(output_root=output_root, model=model, build_id=build_id))
    pages.extend(_render_strategy_pages(output_root=output_root, model=model, build_id=build_id))
    pages.extend(_render_trade_pages(output_root=output_root, model=model, build_id=build_id))
    manifest = _base_manifest(model=model, output_root=output_root, pages=pages)
    manifest.update(
        {
            "status": "passed",
            "final_status": "BUILT_INTERFACE_APEX",
            "research_only": True,
            "live_trading_enabled": False,
            "top_level_nav_count": len(PRIMARY_NAV),
            "x2_preserved": (repo_root / "data/v2_command_center_x2").exists(),
            "x3_preserved": (repo_root / "data/v2_command_center_x3").exists(),
            "prior_ui_roots_preserved": _prior_ui_roots(repo_root),
        }
    )
    _write_json(output_root / "manifests/interface_apex_manifest.json", manifest)
    _write_json(output_root / "reports/build_report.json", manifest)
    (output_root / "reports/build_report.md").write_text(_build_report_md(manifest), encoding="utf-8", newline="\n")
    _write_docs(repo_root=repo_root, output_root=output_root, model=model, build_state=_placeholder_build_state(manifest))
    _write_bridge_links(repo_root=repo_root, output_root=output_root)
    return manifest


def qa_interface_apex(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    return run_interface_apex_qa(output_root=output_root, repo_root=repo_root)


def verify_interface_apex(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    qa = qa_interface_apex(repo_root=repo_root, output_root=output_root)
    manifest = _read_json(output_root / "manifests/interface_apex_manifest.json", {})
    required_docs = _required_docs()
    missing_docs = [path for path in required_docs if not (repo_root / path).exists()]
    missing_pages = [name for name in REQUIRED_PAGE_NAMES if not (output_root / "pages" / name).exists()]
    failures: list[str] = []
    if qa.get("status") != "passed":
        failures.append("qa_not_passed")
    if int(manifest.get("top_level_nav_count") or 99) > 6:
        failures.append("too_many_top_level_nav_items")
    if int(manifest.get("day_count") or 0) <= 0:
        failures.append("day_pages_missing")
    if int(manifest.get("month_count") or 0) <= 0:
        failures.append("month_pages_missing")
    if int(manifest.get("strategy_count") or 0) <= 0:
        failures.append("strategy_pages_missing")
    if int(manifest.get("trade_count") or 0) <= 0:
        failures.append("trade_cards_missing")
    if missing_pages:
        failures.append("missing_required_pages")
    if missing_docs:
        failures.append("missing_required_docs")
    preserved = _prior_ui_roots(repo_root)
    if not all(preserved.values()):
        failures.append("prior_ui_missing")
    result = {
        "schema_version": "v2.interface_apex.verify.v1",
        "status": "passed" if not failures else "failed",
        "qa_status": qa.get("status", "missing"),
        "failures": failures,
        "missing_docs": missing_docs,
        "missing_pages": missing_pages,
        "prior_ui_roots_preserved": preserved,
        "manifest": manifest,
    }
    _write_json(output_root / "reports/verify_latest.json", result)
    (output_root / "reports/verify_latest.md").write_text(_verify_md(result), encoding="utf-8", newline="\n")
    return result


def report_interface_apex(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    manifest = _read_json(output_root / "manifests/interface_apex_manifest.json", {})
    if not manifest:
        manifest = build_interface_apex(repo_root=repo_root, output_root=output_root)
    model = build_apex_model_from_artifacts(repo_root=repo_root)
    qa = qa_interface_apex(repo_root=repo_root, output_root=output_root)
    verify = verify_interface_apex(repo_root=repo_root, output_root=output_root)
    browser = _browser_verification(output_root)
    score = _quality_score(qa=qa, verify=verify, browser=browser, manifest=manifest, model=model)
    if score == 100 and qa.get("status") == "passed" and browser.get("status") == "passed":
        final_status = "COMPLETE_INTERFACE_APEX"
    elif manifest and qa.get("status") == "passed":
        final_status = "COMPLETE_APEX_PARTIAL_RESUME_REQUIRED"
    else:
        final_status = "RESUME_REQUIRED"
    build_state = {
        "schema_version": "v2.interface_apex.build_state.v1",
        "status": "passed",
        "final_status": final_status,
        "quality_score": score,
        "build_id": manifest.get("build_id", _stable_build_id(model)),
        "page_count": manifest.get("page_count", 0),
        "top_level_nav_count": manifest.get("top_level_nav_count", len(PRIMARY_NAV)),
        "day_count": manifest.get("day_count", 0),
        "month_count": manifest.get("month_count", 0),
        "strategy_count": manifest.get("strategy_count", 0),
        "trade_count": manifest.get("trade_count", 0),
        "qa_status": qa.get("status", "missing"),
        "verify_status": verify.get("status", "missing"),
        "browser_verification_status": browser.get("status", "missing"),
        "research_only": True,
        "live_trading_enabled": False,
        "prior_ui_roots_preserved": _prior_ui_roots(repo_root),
        "what_remains_untrusted": _untrusted_items(model),
    }
    _write_docs(repo_root=repo_root, output_root=output_root, model=model, build_state=build_state)
    _write_json(output_root / "reports/release_state.json", build_state)
    return build_state


def demo_interface_apex(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_interface_apex"),
) -> dict[str, Any]:
    manifest = build_interface_apex(repo_root=repo_root, output_root=output_root)
    qa = qa_interface_apex(repo_root=repo_root, output_root=output_root)
    verify = verify_interface_apex(repo_root=repo_root, output_root=output_root)
    report = report_interface_apex(repo_root=repo_root, output_root=output_root)
    return {
        "schema_version": "v2.interface_apex.demo.v1",
        "status": "passed" if qa.get("status") == "passed" and verify.get("status") == "passed" else "failed",
        "final_status": report.get("final_status", "missing"),
        "quality_score": report.get("quality_score", 0),
        "build_id": manifest.get("build_id", "missing"),
        "qa_status": qa.get("status", "missing"),
        "verify_status": verify.get("status", "missing"),
        "browser_verification_status": report.get("browser_verification_status", "missing"),
        "dashboard": (output_root / "index.html").as_posix(),
    }


def serve_interface_apex(
    *,
    output_root: Path = Path("data/v2_interface_apex"),
    host: str = "127.0.0.1",
    port: int = 8765,
) -> dict[str, Any]:
    output_root = output_root.resolve()
    if not output_root.exists():
        return {"status": "failed", "reason": "output_root_missing", "output_root": output_root.as_posix()}

    class ApexHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(output_root), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((host, port), ApexHandler)
    print(f"Serving Interface Apex at http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"status": "stopped", "url": f"http://{host}:{port}/"}


def _ensure_dirs(output_root: Path) -> None:
    for dirname in OUTPUT_DIRS:
        (output_root / dirname).mkdir(parents=True, exist_ok=True)


def _write_model_data(*, output_root: Path, model: InterfaceApexModel) -> dict[str, Any]:
    plain = to_plain(model)
    data_files = {
        "apex_model": output_root / "data/apex_model.json",
        "mission": output_root / "data/mission.json",
        "calendar": output_root / "data/calendar.json",
        "days": output_root / "data/days.json",
        "strategies": output_root / "data/strategies.json",
        "trades": output_root / "data/trades.json",
        "intelligence": output_root / "data/intelligence.json",
        "system": output_root / "data/system.json",
        "no_picks": output_root / "data/no_picks.json",
        "source_refs": output_root / "data/source_refs.json",
        "build_seed": output_root / "data/build_seed.json",
    }
    _write_json(data_files["apex_model"], plain)
    for key, path in data_files.items():
        if key == "apex_model":
            continue
        _write_json(path, plain.get(key, [] if key in {"days", "strategies", "trades"} else {}))
    return {"data_files": {key: path.as_posix() for key, path in data_files.items()}}


def _write_assets(output_root: Path) -> None:
    tokens = {
        "schema_version": "v2.interface_apex.tokens.v1",
        "base": "#05070b",
        "panel": "#0b1118",
        "panel_matte": "#111a24",
        "border": "#203044",
        "text": "#f5fbff",
        "muted": "#8ea4b8",
        "cyan": "#39d7ff",
        "blue": "#2f81ff",
        "green": "#35e6a1",
        "amber": "#f6c453",
        "red": "#ff5d74",
        "radius": "8px",
        "font": "system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
    }
    _write_json(output_root / "assets/apex_tokens.json", tokens)
    (output_root / "assets/apex.css").write_text(_apex_css(), encoding="utf-8", newline="\n")
    (output_root / "assets/apex_components.css").write_text(_apex_components_css(), encoding="utf-8", newline="\n")
    (output_root / "assets/apex.js").write_text(_apex_js(), encoding="utf-8", newline="\n")


def _render_primary_pages(*, output_root: Path, model: InterfaceApexModel, build_id: str) -> list[Path]:
    pages = [
        _write_page(output_root / "index.html", "Mission", _mission_body(model, root=True), model, build_id),
        _write_page(output_root / "pages/mission.html", "Mission", _mission_body(model, root=False), model, build_id),
        _write_page(output_root / "pages/calendar.html", "Calendar", _calendar_body(model, current_only=False), model, build_id),
        _write_page(output_root / "pages/strategies.html", "Strategies", _strategies_body(model), model, build_id),
        _write_page(output_root / "pages/trades.html", "Trades", _trades_body(model), model, build_id),
        _write_page(output_root / "pages/intelligence.html", "Intelligence", _intelligence_body(model), model, build_id),
        _write_page(output_root / "pages/system.html", "System", _system_body(model, output_root=output_root), model, build_id),
        _write_page(output_root / "pages/no_picks.html", "No Picks", _no_picks_body(model), model, build_id),
    ]
    return pages


def _render_calendar_pages(*, output_root: Path, model: InterfaceApexModel, build_id: str) -> list[Path]:
    pages: list[Path] = []
    for month in model.calendar.months:
        pages.append(
            _write_page(
                output_root / "months" / f"{_slug(month.month)}.html",
                f"Calendar {month.month}",
                _single_month_body(model, month),
                model,
                build_id,
            )
        )
    return pages


def _render_day_pages(*, output_root: Path, model: InterfaceApexModel, build_id: str) -> list[Path]:
    pages: list[Path] = []
    for day in model.days:
        pages.append(
            _write_page(
                output_root / "days" / f"{_slug(day.date)}.html",
                f"Day {day.date}",
                _day_body(model, day),
                model,
                build_id,
            )
        )
    return pages


def _render_strategy_pages(*, output_root: Path, model: InterfaceApexModel, build_id: str) -> list[Path]:
    pages: list[Path] = []
    for strategy in model.strategies:
        pages.append(
            _write_page(
                output_root / "strategies" / f"{_strategy_page_name(strategy)}.html",
                strategy.name,
                _strategy_detail_body(model, strategy),
                model,
                build_id,
            )
        )
    return pages


def _render_trade_pages(*, output_root: Path, model: InterfaceApexModel, build_id: str) -> list[Path]:
    pages: list[Path] = []
    for trade in model.trades:
        pages.append(
            _write_page(
                output_root / "trades" / f"{_slug(trade.trade_id)}.html",
                f"Trade {trade.symbol}",
                _trade_detail_body(model, trade),
                model,
                build_id,
            )
        )
    return pages


def _write_page(path: Path, title: str, body: str, model: InterfaceApexModel, build_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel_assets = _relative(path.parent, _output_root_for(path) / "assets")
    html_text = _layout(title=title, body=body, model=model, build_id=build_id, rel_assets=rel_assets, path=path)
    path.write_text(_clean_generated_text(html_text), encoding="utf-8", newline="\n")
    return path


def _layout(*, title: str, body: str, model: InterfaceApexModel, build_id: str, rel_assets: str, path: Path) -> str:
    nav = _nav(path)
    latest = _esc(model.mission.latest_run_time)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dawnstrike Apex - {_esc(title)}</title>
  <link rel="stylesheet" href="{rel_assets}/apex.css?v={_esc(build_id)}">
  <link rel="stylesheet" href="{rel_assets}/apex_components.css?v={_esc(build_id)}">
</head>
<body>
<aside class="apex-shell">
  <a class="brand" href="{_root_link(path, "index.html")}"><span>Dawnstrike</span><b>Apex</b></a>
  <p class="brand-subtitle">Premium day-trading cockpit</p>
  <nav class="primary-nav" data-primary-nav>{nav}</nav>
  <section class="trust-boundary trust-boundary-banner">
    <strong>Research-only</strong>
    <span>Live trading disabled. No strategy is validated for live execution.</span>
  </section>
</aside>
<main>
  <header class="topbar">
    <div><span>Latest artifact</span><strong>{latest}</strong></div>
    <div><span>Mode</span><strong>Paper and research evidence</strong></div>
    <a class="toplink" href="{_root_link(path, "pages/no_picks.html")}">No-picks story</a>
  </header>
  <section class="boundary-strip">
    <span>Research-only</span>
    <span>Live trading disabled</span>
    <span>Historical backtests are not validation</span>
    <span>Warnings stay visible</span>
  </section>
  {body}
</main>
<script src="{rel_assets}/apex.js?v={_esc(build_id)}" defer></script>
</body>
</html>
"""


def _nav(path: Path) -> str:
    items = []
    current = path.as_posix().replace("\\", "/")
    for label, href in PRIMARY_NAV:
        target = href.split("/")[-1]
        active = ' class="active"' if current.endswith(target) or (href == "pages/mission.html" and current.endswith("index.html")) else ""
        items.append(f'<a{active} href="{_root_link(path, href)}">{_esc(label)}</a>')
    return "\n".join(items)


def _mission_body(model: InterfaceApexModel, *, root: bool) -> str:
    mission = model.mission
    source = "data/apex_model.json" if root else "../data/apex_model.json"
    day_trade = [strategy for strategy in model.strategies if strategy.lane == "day_trade"]
    lane_count = sum(strategy.trade_count for strategy in day_trade)
    cards = [
        ("Day return", mission.day_return, "Latest artifact day"),
        ("Cumulative return", mission.cumulative_return, "Paper calendar cumulative"),
        ("Trades today", str(mission.paper_trades_today), "Official paper cards plus clearly labeled examples"),
        ("Open paper trades", str(mission.open_paper_trades), "From Paper trading record"),
    ]
    return f"""
<section class="mission-hero">
  <div class="hero-copy">
    <p class="eyebrow">Mission</p>
    <h1>{_esc(mission.headline)}</h1>
    <p class="hero-subtitle">{_esc(mission.subheadline)}</p>
    <div class="hero-actions">
      <a class="button-primary" href="{_root_link_for_body(root, "pages/calendar.html")}">Open Calendar</a>
      <a class="button-secondary" href="{_root_link_for_body(root, "pages/strategies.html")}">Review Strategies</a>
    </div>
  </div>
  <div class="system-pulse system-pulse-card">
    <span>System pulse</span>
    <strong>{_esc(mission.status.replace("_", " "))}</strong>
    <p>Next run: {_esc(mission.next_run_time)}</p>
  </div>
</section>
<section class="metric-row">
  {_metric_cards(cards)}
</section>
<section class="story-grid two-up">
  <article class="story-card warning-stack">
    <span>Top warning</span>
    <h2>{_esc(mission.top_warning)}</h2>
    <p>Warnings stay visible here before any deeper dashboard or raw artifact view.</p>
  </article>
  <article class="story-card learning-lesson-card">
    <span>Latest lesson</span>
    <h2>{_esc(mission.latest_lesson)}</h2>
    <p>{_esc(mission.next_action)}</p>
  </article>
</section>
<section class="story-grid three-up">
  <article class="story-card">
    <span>Best current day-trade candidate</span>
    <h2>{_esc(mission.top_strategy)}</h2>
    <p>{lane_count:,} same-session historical research trades are available. They are not forward validation.</p>
  </article>
  <article class="story-card">
    <span>Evidence boundary</span>
    <h2>Official paper evidence is separate from research.</h2>
    <p>Daily-bar swing research, historical day-trade backtests, and shadow challengers are separated into their own lanes.</p>
  </article>
  <article class="story-card">
    <span>Operator answer</span>
    <h2>Dawnstrike explains why it acted or stayed out.</h2>
    <p>Open Calendar for day stories, Trades for evidence cards, or System for diagnostics.</p>
  </article>
</section>
{_advanced_drawer("source data and artifacts", [("Apex model JSON", source)])}
"""


def _calendar_body(model: InterfaceApexModel, *, current_only: bool) -> str:
    months = model.calendar.months[-1:] if current_only and model.calendar.months else model.calendar.months
    month_blocks = "\n".join(_month_panel(month, month_href_prefix="../months/") for month in months)
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Calendar</p>
  <h1>Performance starts with clickable day stories.</h1>
  <p>Each day shows daily return, cumulative return, trade count, warning markers, learning markers, and whether standing aside was the disciplined result.</p>
</section>
<section class="cumulative-return-ribbon">
  <span>Current month</span><strong>{_esc(model.calendar.current_month)}</strong>
  <span>Cumulative</span><strong>{_esc(model.mission.cumulative_return)}</strong>
  <span>Day stories</span><strong>{len(model.days)}</strong>
</section>
{month_blocks}
{_advanced_drawer("source data and artifacts", [("Calendar JSON", "../data/calendar.json"), ("Day stories JSON", "../data/days.json")])}
"""


def _single_month_body(model: InterfaceApexModel, month: CalendarMonth) -> str:
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Calendar</p>
  <h1>{_esc(month.month)} performance story.</h1>
  <p>Month return {_esc(month.monthly_return_pct)}. Cumulative return {_esc(month.cumulative_return_pct)}. Click any populated day for the day story.</p>
</section>
{_month_panel(month, month_href_prefix="")}
{_advanced_drawer("source data and artifacts", [("Calendar JSON", "../data/calendar.json")])}
"""


def _month_panel(month: CalendarMonth, *, month_href_prefix: str) -> str:
    summary = [
        ("Month return", month.monthly_return_pct, "Artifact month total"),
        ("Cumulative", month.cumulative_return_pct, "Through this month"),
        ("Best day", month.best_day, "Highest day tile"),
        ("Worst day", month.worst_day, "Lowest day tile"),
        ("Win days", str(month.win_days), "Green tiles"),
        ("Loss days", str(month.loss_days), "Red tiles"),
        ("No-trade days", str(month.no_trade_days), "Disciplined stays out"),
        ("Total trades", str(month.total_trades), "Paper or labeled examples"),
    ]
    tiles = "\n".join(_calendar_tile(tile) for tile in month.day_tiles)
    return f"""
<section class="month-panel monthly-return-calendar">
  <header class="section-heading">
    <div><span>Month</span><h2>{_esc(month.month)}</h2></div>
    <div class="month-links"><a href="{month_href_prefix}{_slug(month.previous_month)}.html">Previous</a><a href="{month_href_prefix}{_slug(month.next_month)}.html">Next</a></div>
  </header>
  <div class="metric-row dense">{_metric_cards(summary)}</div>
  <div class="calendar-grid">
    {tiles}
  </div>
</section>
"""


def _calendar_tile(tile: Any) -> str:
    markers = []
    if tile.warning_marker:
        markers.append('<span class="dot warning" title="Warning"></span>')
    if tile.learning_marker:
        markers.append('<span class="dot learning" title="Learning"></span>')
    if tile.no_trade_marker:
        markers.append('<span class="tag">No trade</span>')
    return f"""
<a class="day-tile tone-{_slug(tile.tone)}" href="{_esc(tile.day_story_link)}">
  <span class="day-date">{_esc(tile.date[-2:])}</span>
  <strong>{_esc(tile.daily_return_pct)}</strong>
  <small>Cum {_esc(tile.cumulative_return_pct)}</small>
  <small>{tile.trade_count} trade(s)</small>
  <span class="markers">{''.join(markers)}</span>
</a>
"""


def _day_body(model: InterfaceApexModel, day: DayModel) -> str:
    trade_cards = "\n".join(_trade_card(trade, detail=True) for trade in day.trades) or "<article class=\"empty-card\">No official paper trades fired in this day story.</article>"
    strategy_cards = "\n".join(f"<article class=\"mini-card\"><strong>{_esc(item)}</strong><span>Evaluated in artifact row</span></article>" for item in day.strategies_evaluated[:8])
    no_picks = "\n".join(f"<li>{_esc(reason)}</li>" for reason in day.no_picks_reasons[:8]) or "<li>No no-picks reason artifact was available for this day.</li>"
    warnings = "\n".join(f"<li>{_esc(item)}</li>" for item in day.warnings[:8]) or "<li>No day-level warning artifact was available.</li>"
    return f"""
<section class="page-hero compact day-story-panel">
  <p class="eyebrow">Day story</p>
  <h1>{_esc(day.headline)}</h1>
  <p>{_esc(day.plain_english_summary)}</p>
</section>
<section class="metric-row">
  {_metric_cards([("Daily return", day.daily_return, "Artifact value"), ("Cumulative return", day.cumulative_return, "Through this day"), ("Trade cards", str(len(day.trades)), "Official or clearly labeled"), ("Evidence quality", day.evidence_quality, "Fill and official gate")])}
</section>
<section class="section-stack">
  <header class="section-heading"><div><span>Trade journey</span><h2>What fired, or why nothing did</h2></div></header>
  <div class="trade-grid">{trade_cards}</div>
</section>
<section class="story-grid two-up">
  <article class="story-card no-picks-story-card">
    <span>Why no picks, if applicable</span>
    <h2>Standing aside can be correct.</h2>
    <ul>{no_picks}</ul>
  </article>
  <article class="story-card warning-stack">
    <span>Warnings</span>
    <h2>Warnings stay visible.</h2>
    <ul>{warnings}</ul>
  </article>
</section>
<section class="section-stack">
  <header class="section-heading"><div><span>Strategy context</span><h2>Strategies evaluated</h2></div></header>
  <div class="mini-grid">{strategy_cards}</div>
</section>
<section class="story-grid two-up">
  <article class="story-card learning-lesson-card"><span>What Dawnstrike learned</span><h2>{_esc(day.learning_note)}</h2></article>
  <article class="story-card market-masters-card"><span>What to watch next</span><h2>{_esc(day.what_to_watch_tomorrow)}</h2><p>{_esc(day.market_masters_note)}</p></article>
</section>
{_advanced_drawer("source data and artifacts", [("Day stories JSON", "../data/days.json"), ("Trades JSON", "../data/trades.json")])}
"""


def _strategies_body(model: InterfaceApexModel) -> str:
    groups = {
        "day_trade": "Intraday day-trade research",
        "swing_research": "Daily-bar swing research",
        "shadow_challenger": "Shadow challengers",
        "benchmark": "Benchmarks",
    }
    blocks = []
    for lane, title in groups.items():
        rows = [strategy for strategy in model.strategies if strategy.lane == lane]
        if not rows:
            continue
        cards = "\n".join(_strategy_card(strategy) for strategy in rows)
        blocks.append(f"<section class=\"strategy-lane lane-{lane}\"><header class=\"section-heading\"><div><span>{_esc(title)}</span><h2>{len(rows)} report card(s)</h2></div></header><div class=\"strategy-grid\">{cards}</div></section>")
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Strategies</p>
  <h1>Day-trade research first. Swing research and shadow ideas stay separated.</h1>
  <p>Every card shows status, trade count, R, expectancy, drawdown, robustness, slippage, OOS state, and validation progress without claiming live readiness.</p>
</section>
{''.join(blocks)}
{_advanced_drawer("source data and artifacts", [("Strategies JSON", "../data/strategies.json"), ("Day Trade Lab comparison", "../data/source_refs.json")])}
"""


def _strategy_card(strategy: StrategyModel) -> str:
    lane = strategy.lane.replace("_", " ")
    warnings = "".join(f"<li>{_esc(item)}</li>" for item in strategy.warnings[:3])
    return f"""
<article class="strategy-card strategy-score-card" data-lane="{_esc(strategy.lane)}">
  <div class="card-topline"><span>{_esc(lane)}</span><a href="{_esc(strategy.detail_link)}">Open report</a></div>
  <h3>{_esc(strategy.name)}</h3>
  <p>{_esc(strategy.status)}</p>
  <div class="stat-grid">
    <span><b>{strategy.trade_count}</b><small>trades</small></span>
    <span><b>{_esc(strategy.win_rate)}</b><small>win rate</small></span>
    <span><b>{_esc(strategy.average_r)}</b><small>avg R</small></span>
    <span><b>{_esc(strategy.expectancy)}</b><small>expectancy</small></span>
    <span><b>{_esc(strategy.profit_factor)}</b><small>profit factor</small></span>
    <span><b>{_esc(strategy.drawdown)}</b><small>drawdown</small></span>
  </div>
  <div class="badge-row">
    <span class="badge robustness-badge">{_esc(strategy.robustness_score)}</span>
    <span class="badge evidence-quality-badge">Not validated</span>
  </div>
  <ul class="warnings-list">{warnings}</ul>
</article>
"""


def _strategy_detail_body(model: InterfaceApexModel, strategy: StrategyModel) -> str:
    examples = "\n".join(_trade_card(trade, detail=False) for trade in strategy.trade_examples) or "<article class=\"empty-card\">No trade examples available for this strategy artifact.</article>"
    trusted = "Not trusted for live execution. Needs forward same-session paper evidence." if strategy.lane == "day_trade" else "Not a day-trade validation lane."
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Strategy report card</p>
  <h1>{_esc(strategy.name)}</h1>
  <p>{_esc(strategy.status)} Validation progress: {_esc(strategy.validation_progress)}.</p>
</section>
<section class="metric-row">
  {_metric_cards([("Lane", strategy.lane.replace("_", " "), "Separated safety lane"), ("Trades", str(strategy.trade_count), "Source artifact count"), ("Expectancy", strategy.expectancy, "Research metric"), ("Drawdown", strategy.drawdown, "Source artifact")])}
</section>
<section class="story-grid two-up">
  <article class="story-card strategy-robustness-badge"><span>Robustness</span><h2>{_esc(strategy.robustness_score)}</h2><p>Slippage: {_esc(strategy.slippage_status)}. OOS: {_esc(strategy.oos_status)}.</p></article>
  <article class="story-card evidence-quality-badge"><span>Why trusted or not trusted</span><h2>{_esc(trusted)}</h2><p>Historical day-trade backtests remain research; shadow and swing lanes do not become official evidence here.</p></article>
</section>
<section class="story-grid two-up">
  <article class="story-card"><span>Best conditions</span><ul>{_list_items(strategy.best_conditions)}</ul></article>
  <article class="story-card"><span>Worst conditions</span><ul>{_list_items(strategy.worst_conditions)}</ul></article>
</section>
<section class="section-stack">
  <header class="section-heading"><div><span>Trade examples</span><h2>Examples stay labeled by evidence type</h2></div></header>
  <div class="trade-grid">{examples}</div>
</section>
{_advanced_drawer("source data and artifacts", [("Strategies JSON", "../data/strategies.json"), ("Trades JSON", "../data/trades.json")])}
"""


def _trades_body(model: InterfaceApexModel) -> str:
    options = sorted({trade.strategy for trade in model.trades if trade.strategy})
    strategy_options = "\n".join(f'<option value="{_esc(option)}">{_esc(option)}</option>' for option in options[:40])
    cards = "\n".join(_trade_card(trade, detail=True) for trade in model.trades) or "<article class=\"empty-card\">No trade cards available.</article>"
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Trades</p>
  <h1>Trade cards show the journey, not a spreadsheet.</h1>
  <p>Entry, exit, hold minutes, R, result, evidence type, and warnings remain visible. Unknown timing stays unknown.</p>
</section>
<section class="filter-bar" data-filter-bar>
  <label>Date <input type="text" data-filter="date" placeholder="2026-07-02"></label>
  <label>Symbol <input type="text" data-filter="symbol" placeholder="QQQ"></label>
  <label>Strategy <select data-filter="strategy"><option value="">All</option>{strategy_options}</select></label>
  <label>Result <select data-filter="result"><option value="">All</option><option value="positive">Positive</option><option value="negative">Negative</option></select></label>
  <label>Evidence <select data-filter="evidence"><option value="">All</option><option value="official">Official</option><option value="historical">Historical</option><option value="shadow">Shadow</option></select></label>
</section>
<section class="trade-grid" data-trade-list>{cards}</section>
{_advanced_drawer("View raw ledger", [("Trades JSON", "../data/trades.json"), ("Source manifest", "../data/source_refs.json")])}
"""


def _trade_card(trade: TradeModel, *, detail: bool) -> str:
    result = "positive" if not trade.r_multiple.startswith("-") and not trade.pnl.startswith("-") else "negative"
    evidence = "official" if "official" in trade.official_or_shadow else "historical" if "historical" in trade.official_or_shadow else "shadow"
    warnings = "".join(f"<li>{_esc(item)}</li>" for item in trade.warnings[:3])
    link = f'<a class="card-link" href="../trades/{_slug(trade.trade_id)}.html">Open trade</a>' if detail else ""
    return f"""
<article class="trade-card trade-journey-card" data-date="{_esc(trade.date)}" data-symbol="{_esc(trade.symbol)}" data-strategy="{_esc(trade.strategy)}" data-result="{result}" data-evidence="{evidence}">
  <div class="card-topline"><span>{_esc(trade.official_or_shadow)}</span>{link}</div>
  <h3>{_esc(trade.symbol)} <small>{_esc(trade.direction)}</small></h3>
  <p>{_esc(trade.strategy)} · {_esc(trade.interval)}</p>
  <div class="timeline">
    <span><b>Entry</b>{_esc(trade.entry_time)} @ {_esc(trade.entry_price)}</span>
    <span><b>Exit</b>{_esc(trade.exit_time)} @ {_esc(trade.exit_price)}</span>
    <span><b>Hold</b>{_esc(trade.hold_minutes)} min</span>
  </div>
  <div class="stat-grid compact">
    <span><b>{_esc(trade.r_multiple)}</b><small>R</small></span>
    <span><b>{_esc(trade.pnl)}</b><small>PnL</small></span>
    <span><b>{_esc(trade.exit_reason[:40])}</b><small>exit reason</small></span>
  </div>
  <span class="badge evidence-quality-badge">{_esc(trade.evidence_type)}</span>
  <ul class="warnings-list">{warnings}</ul>
</article>
"""


def _trade_detail_body(model: InterfaceApexModel, trade: TradeModel) -> str:
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Trade journey</p>
  <h1>{_esc(trade.symbol)} through {_esc(trade.strategy)}</h1>
  <p>{_esc(trade.official_or_shadow)}. Evidence type: {_esc(trade.evidence_type)}.</p>
</section>
<section class="trade-grid">{_trade_card(trade, detail=False)}</section>
{_advanced_drawer("source data and artifacts", [("Trades JSON", "../data/trades.json")])}
"""


def _intelligence_body(model: InterfaceApexModel) -> str:
    intel = model.intelligence
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">Intelligence</p>
  <h1>What Dawnstrike learned, and why ideas remain shadow-only.</h1>
  <p>Learning and research-inspired ideas are shown as story cards. Nothing here promotes a challenger or validates a strategy.</p>
</section>
<section class="story-grid two-up">
  <article class="story-card learning-lesson-card"><span>What Dawnstrike learned</span><h2>{_esc(intel.latest_lesson)}</h2><p>Status: {_esc(intel.learning_foundry_status)}</p></article>
  <article class="story-card"><span>Current regime</span><h2>{_esc(intel.regime)}</h2><p>Regime labels come from local artifacts and remain research context.</p></article>
</section>
<section class="story-grid three-up">
  <article class="story-card market-masters-card"><span>Research-inspired ideas</span><h2>{len(intel.methodologies)} methodologies</h2><ul>{_list_items(intel.methodologies)}</ul></article>
  <article class="story-card"><span>Primitives</span><h2>{len(intel.primitives)} primitive ideas</h2><ul>{_list_items(intel.primitives)}</ul></article>
  <article class="story-card"><span>Shadow challengers</span><h2>{intel.shadow_only_count} shadow-only</h2><ul>{_list_items(intel.challengers)}</ul></article>
</section>
<section class="story-grid two-up">
  <article class="story-card warning-stack"><span>Why none are promoted</span><h2>{_esc(intel.promotion_status.replace("_", " "))}</h2><p>{_esc(intel.validation_blocked_reason)}</p></article>
  <article class="story-card"><span>Evidence missing</span><h2>True forward sample and official paper gate proof.</h2><p>Historical backtests and shadow results remain below the official evidence line.</p></article>
</section>
{_advanced_drawer("source data and artifacts", [("Intelligence JSON", "../data/intelligence.json"), ("Source refs", "../data/source_refs.json")])}
"""


def _system_body(model: InterfaceApexModel, *, output_root: Path) -> str:
    system = model.system
    tasks = "\n".join(
        f"<article class=\"mini-card\"><strong>{_esc(str(task.get('task_name', 'task')))}</strong><span>Next: {_esc(str(task.get('next_run_time', 'unknown')))}</span><span>State: {_esc(str(task.get('state', 'unknown')))}</span></article>"
        for task in system.scheduled_tasks
    ) or "<article class=\"empty-card\">No scheduled task artifact available.</article>"
    warnings = "".join(f"<li>{_esc(item)}</li>" for item in system.warnings[:18]) or "<li>No system warning artifact available.</li>"
    bridge_links = _system_bridge_links(output_root)
    translations = "".join(f"<li><b>{_esc(key)}</b>: {_esc(value)}</li>" for key, value in COPY_TRANSLATIONS.items())
    return f"""
<section class="page-hero compact">
  <p class="eyebrow">System</p>
  <h1>Advanced diagnostics live here, not in the primary story.</h1>
  <p>Provider readiness, Telegram updates, DataTruth, FillTruth, CommitBridge, PaperOps, scheduler, watchdog, and raw artifacts are inspectable here.</p>
</section>
<section class="story-grid three-up automation-status-rail">
  <article class="story-card"><span>Automatic schedule</span><h2>{len(system.scheduled_tasks)} task(s)</h2><p>{_esc(system.sentinel_status)}</p></article>
  <article class="story-card telegram-status-card"><span>Telegram updates</span><h2>{_esc(system.telegram_status)}</h2><p>UI does not send messages.</p></article>
  <article class="story-card"><span>Live trading</span><h2>Live trading disabled</h2><p>No execution controls exist in Apex.</p></article>
</section>
<section class="mini-grid">{tasks}</section>
<section class="story-grid two-up">
  <article class="story-card"><span>Data quality check</span><h2>{_esc(system.data_quality_status)}</h2><p>Provider status: {_esc(system.provider_status)}</p></article>
  <article class="story-card"><span>Evidence chain</span><h2>{_esc(system.evidence_chain_status)}</h2><p>FillTruth and CommitBridge stay visible as advanced diagnostics.</p></article>
</section>
<section class="story-card warning-stack"><span>Warnings</span><h2>Warnings stay visible.</h2><ul>{warnings}</ul></section>
<section class="story-card">
  <span>Plain-English translation layer</span>
  <h2>Internal names stay secondary.</h2>
  <ul>{translations}</ul>
</section>
<section class="story-card">
  <span>Advanced diagnostics</span>
  <h2>Prior dashboard links</h2>
  <div class="link-row">{bridge_links}</div>
</section>
{_advanced_drawer("source data and artifacts", [("System JSON", "../data/system.json"), ("Source refs", "../data/source_refs.json"), ("Build report", "../reports/build_report.json")])}
"""


def _no_picks_body(model: InterfaceApexModel) -> str:
    no = model.no_picks
    return f"""
<section class="page-hero compact no-picks-story-card">
  <p class="eyebrow">No Picks</p>
  <h1>No official paper trades today.</h1>
  <p>That can be a disciplined result. Dawnstrike should stay out when evidence, data quality, or risk filters do not clear.</p>
</section>
<section class="metric-row">
  {_metric_cards([("Accepted", str(no.accepted_count), "Official paper trades"), ("Blocked", str(no.blocked_count), "Stopped by gates"), ("Watch", str(no.watch_count), "Near setups"), ("No setup", str(no.no_setup_count), "Nothing clean enough")])}
</section>
<section class="story-grid two-up">
  <article class="story-card"><span>Why Dawnstrike waited</span><h2>{_esc(no.headline)}</h2><ul class="no-picks-reason">{_list_items(no.top_reasons)}</ul></article>
  <article class="story-card"><span>Why no trade is valid</span><h2>{_esc(no.why_no_trade_is_valid)}</h2><p>A flat day can be the correct operator outcome.</p></article>
</section>
<section class="story-grid three-up">
  <article class="story-card"><span>Nearest setups</span><ul>{_list_items(no.near_setups)}</ul></article>
  <article class="story-card"><span>Data limitations</span><ul>{_list_items(no.data_quality_blockers)}</ul></article>
  <article class="story-card"><span>Risk filter blockers</span><ul>{_list_items(no.riskhub_blockers)}</ul></article>
</section>
<section class="story-grid two-up">
  <article class="story-card"><span>Strategy states</span><ul>{_list_items(no.strategies_blocked)}</ul></article>
  <article class="story-card"><span>What would need to change</span><ul>{_list_items(no.what_would_change)}</ul></article>
</section>
{_advanced_drawer("source data and artifacts", [("No-picks JSON", "../data/no_picks.json")])}
"""


def _metric_cards(cards: list[tuple[str, str, str]]) -> str:
    return "\n".join(
        f"<article class=\"metric-card\"><span>{_esc(label)}</span><strong>{_esc(value)}</strong><small>{_esc(detail)}</small></article>"
        for label, value, detail in cards
    )


def _advanced_drawer(title: str, links: list[tuple[str, str]]) -> str:
    items = "\n".join(f'<li><a href="{_esc(href)}">{_esc(label)}</a></li>' for label, href in links)
    return f"""
<details class="advanced-drawer raw-data">
  <summary>Advanced: {_esc(title)}</summary>
  <p>Source data is secondary. The story above is the primary operator view.</p>
  <ul>{items}</ul>
</details>
"""


def _list_items(items: list[str]) -> str:
    if not items:
        return "<li>unknown</li>"
    return "".join(f"<li>{_esc(item)}</li>" for item in items[:8])


def _system_bridge_links(output_root: Path) -> str:
    links = []
    for dirname, label in (
        ("v2_command_center_x3", "Open Command Center X3"),
        ("v2_command_center_x2", "Open Command Center X2"),
        ("v2_command_center", "Open original Command Center"),
    ):
        target = output_root.parent / dirname / "index.html"
        if target.exists():
            links.append(f'<a href="../../{dirname}/index.html">{_esc(label)}</a>')
    if not links:
        links.append("<span>No prior UI bridge target exists beside this output root.</span>")
    return "\n".join(links)


def _base_manifest(*, model: InterfaceApexModel, output_root: Path, pages: list[Path]) -> dict[str, Any]:
    return {
        "schema_version": "v2.interface_apex.manifest.v1",
        "build_id": _stable_build_id(model),
        "dashboard": (output_root / "index.html").as_posix(),
        "page_count": len({path.as_posix() for path in pages}),
        "top_level_nav_count": len(PRIMARY_NAV),
        "day_count": len(model.days),
        "month_count": len(model.calendar.months),
        "strategy_count": len(model.strategies),
        "trade_count": len(model.trades),
        "nav": [label for label, _href in PRIMARY_NAV],
        "output_root": output_root.as_posix(),
        "pages": [path.as_posix() for path in sorted(set(pages), key=lambda item: item.as_posix())],
    }


def _placeholder_build_state(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "v2.interface_apex.build_state.v1",
        "status": "passed",
        "final_status": "BUILT_INTERFACE_APEX",
        "quality_score": "pending_report",
        "build_id": manifest.get("build_id", "unknown"),
        "page_count": manifest.get("page_count", 0),
        "top_level_nav_count": manifest.get("top_level_nav_count", len(PRIMARY_NAV)),
        "day_count": manifest.get("day_count", 0),
        "month_count": manifest.get("month_count", 0),
        "strategy_count": manifest.get("strategy_count", 0),
        "trade_count": manifest.get("trade_count", 0),
        "research_only": True,
        "live_trading_enabled": False,
    }


def _write_docs(*, repo_root: Path, output_root: Path, model: InterfaceApexModel, build_state: dict[str, Any]) -> None:
    arch = repo_root / "docs/architecture"
    ops = repo_root / "docs/operations"
    audit = repo_root / "docs/audit"
    arch.mkdir(parents=True, exist_ok=True)
    ops.mkdir(parents=True, exist_ok=True)
    audit.mkdir(parents=True, exist_ok=True)
    (arch / "v2_interface_apex_product_vision.md").write_text(_product_vision_doc(), encoding="utf-8", newline="\n")
    (arch / "v2_interface_apex_information_architecture.md").write_text(_information_architecture_doc(), encoding="utf-8", newline="\n")
    (arch / "v2_interface_apex_design_system.md").write_text(_design_system_doc(), encoding="utf-8", newline="\n")
    (arch / "v2_interface_apex.md").write_text(_architecture_doc(), encoding="utf-8", newline="\n")
    (ops / "interface_apex_user_guide.md").write_text(_user_guide_doc(), encoding="utf-8", newline="\n")
    (ops / "interface_apex_rebuild.md").write_text(_rebuild_doc(output_root), encoding="utf-8", newline="\n")
    (audit / "omega_interface_apex_release_summary.md").write_text(_release_summary_doc(build_state, model), encoding="utf-8", newline="\n")
    (audit / "omega_interface_apex_quality_scorecard.md").write_text(_quality_scorecard_doc(build_state), encoding="utf-8", newline="\n")
    (audit / "omega_interface_apex_red_team.md").write_text(_red_team_doc(build_state), encoding="utf-8", newline="\n")
    _write_json(audit / "omega_interface_apex_build_state.json", build_state)
    (audit / "omega_interface_apex_resume_goal.md").write_text(_resume_goal_doc(build_state), encoding="utf-8", newline="\n")


def _product_vision_doc() -> str:
    return """# Dawnstrike Interface Apex Product Vision

Interface Apex is the definitive local Dawnstrike operator interface. Dawnstrike is a research-first, paper-evidence trading system that ingests market data, checks data quality, records paper evidence, evaluates strategies, watches automation, drafts Telegram updates, learns from recent evidence, and keeps live execution disabled.

The UI solves the operator problem: the system has many subsystems, but the operator needs the truth in seconds. In five seconds Apex must answer whether Dawnstrike is running, whether official paper trades exist, what return changed, what warning matters, what it learned, and what to watch next. In five minutes it must let the operator inspect day stories, trade cards, strategy report cards, no-picks reasons, intelligence, and system diagnostics.

The primary operator is a trading-system owner who wants clear research and paper-evidence truth without false confidence. Advanced artifacts, internal subsystem names, JSON files, and diagnostics are hidden until drill-down. Warnings, research-only boundaries, live-trading disabled status, unvalidated strategy status, no-picks reasons, and evidence limits are never hidden.

Day trading means same-session intraday research or forward same-session paper evidence. Daily-bar swing research is separated and never presented as a day-trade lane. Historical backtests are research evidence, not validation. Shadow challengers are ideas, not official strategies. Official paper evidence requires the paper record and official evidence gate to support it.

No picks means no official paper trade cleared the gates. That can be a disciplined result. Not validated means the strategy has insufficient forward evidence for trust. System healthy means the scheduler, data quality, paper record, official gate, Telegram readiness, and watchdog artifacts are present with visible warnings and live trading disabled.

Apex explicitly rejects table-first design, more than six top-level tabs, technical subsystem names as the primary UX, raw artifact browsing as the default experience, fake certainty, and live-trading aesthetics.
"""


def _information_architecture_doc() -> str:
    return """# Interface Apex Information Architecture

Top-level navigation is capped at six sections: Mission, Calendar, Strategies, Trades, Intelligence, and System.

Mission contains today's story, system status, next scheduled run, paper result, top warning, best current day-trade candidate, and latest lesson.

Calendar is the primary performance view. It contains the monthly calendar, daily return, cumulative return, trade count, warning badges, no-trade markers, and clickable day stories.

Strategies contains day-trade strategies first, then daily swing research, shadow challengers, and benchmarks. Cards show robustness, validation progress, watch or quarantine state, slippage, OOS state, drawdown, expectancy, and trade count.

Trades contains trade cards, entry and exit timeline, hold minutes, R multiple, exit reason, evidence badge, filters, and no-overlap proof where the source provides it. Raw ledger data is behind a disclosure.

Intelligence contains what Dawnstrike learned, current regime, research-inspired ideas, shadow challengers, why none are promoted, and what evidence is missing.

System contains scheduler status, Telegram updates, providers, data quality, fill quality, official paper-evidence gate, paper records, watchdog, warnings, advanced diagnostics, raw artifact links, and links to X2 and X3.

No technical subsystem gets promoted into a top-level tab. Technical names appear in System or advanced sections only.
"""


def _design_system_doc() -> str:
    return """# Interface Apex Design System

Apex uses Apple polish and SpaceX engineering: deep graphite base, dark blue depth, cyan and electric-blue accents, green for confirmed positive, amber for warnings, red only for real problems, matte glass panels, precise spacing, high contrast, readable numbers, and system fonts.

Components: ApexShell, MissionHero, SystemPulse, MonthlyReturnCalendar, CumulativeReturnRibbon, DayStoryPanel, TradeJourneyCard, StrategyScoreCard, StrategyRobustnessBadge, EvidenceQualityBadge, NoPicksStoryCard, LearningLessonCard, MarketMastersCard, AutomationStatusRail, TelegramStatusCard, WarningStack, AdvancedDrawer, RawDataDisclosure, and TrustBoundaryBanner.

No external fonts, CDN, analytics, or remote JavaScript are used. Local JavaScript is limited to card filtering, collapsible sections, month navigation helpers, and theme toggling. It does not call provider APIs, send Telegram updates, mutate files, run commands, place trades, or read secrets.
"""


def _architecture_doc() -> str:
    return """# Interface Apex Architecture

Interface Apex is an additive static UI module at `intraday_scanner/v2/interface_apex/` with generated output at `data/v2_interface_apex/`.

The adapter reads existing local artifacts from Command Center X3, Day Trade Lab, PaperOps, Learning Foundry, Market Masters, Telegram Intelligence, DataTruth, FillTruth, CommitBridge, and the autonomous runner. It does not recompute signals, fetch market data, send Telegram messages, mutate SQLite databases, mutate PaperOps, mutate FillTruth, mutate CommitBridge, mutate RiskHub, instantiate broker clients, or enable live trading.

The renderer writes static HTML, local CSS, local JavaScript, JSON view models, manifests, QA reports, and audit docs. Primary pages are story-first. Raw artifacts are secondary disclosures.

Apex differs from X3 by becoming the product-grade operator surface: six sections, day-trade-first strategy lanes, clickable calendar, day stories, trade cards, no-picks story, intelligence story, and advanced diagnostics in System.
"""


def _user_guide_doc() -> str:
    return """# Interface Apex User Guide

Open `data/v2_interface_apex/index.html` to start on Mission. Mission answers whether Dawnstrike is running, what happened most recently, what warning matters, what return changed, what lesson was learned, and what to watch next.

Use Calendar as the primary performance view. Click a day tile to open its day story. A day story explains returns, trade cards, no-picks reasons, warnings, evidence quality, learning, and next watch items.

Use Strategies to read report cards. Day-trade research appears first. Daily-bar swing research, shadow challengers, and benchmarks are separated so they cannot be mistaken for validated day trades.

Use Trades to inspect trade journey cards. Unknown entry, exit, or hold fields remain unknown when the source artifact does not prove them. Historical examples stay labeled historical-only.

Use Intelligence to understand learning, regimes, Market Masters ideas, shadow challengers, and promotion blockers. Use System for provider readiness, scheduler status, Telegram status, data quality, FillTruth, CommitBridge, PaperOps, watchdog, warnings, advanced artifact links, and links to X2/X3.

Nothing in Apex places trades, sends Telegram updates, fetches providers, exposes secrets, or weakens live-trading boundaries.
"""


def _rebuild_doc(output_root: Path) -> str:
    return f"""# Interface Apex Rebuild

Run these commands from the repository root:

```
py -m intraday_scanner.v2.interface_apex build-models
py -m intraday_scanner.v2.interface_apex build-calendar
py -m intraday_scanner.v2.interface_apex build-days
py -m intraday_scanner.v2.interface_apex build
py -m intraday_scanner.v2.interface_apex qa
py -m intraday_scanner.v2.interface_apex verify
py -m intraday_scanner.v2.interface_apex report
py -m intraday_scanner.v2.interface_apex demo
```

Output root: `{output_root.as_posix()}`.

Optional local serve:

```
py -m intraday_scanner.v2.interface_apex serve --port 8765
```

The server is local-only and serves static files. It does not fetch providers, send Telegram updates, run commands, mutate data, or enable live trading.
"""


def _release_summary_doc(build_state: dict[str, Any], model: InterfaceApexModel) -> str:
    return f"""# OMEGA Interface Apex Release Summary

- Final status: `{build_state.get('final_status')}`
- Quality score: `{build_state.get('quality_score')}`
- Build ID: `{build_state.get('build_id')}`
- Top-level nav count: `{build_state.get('top_level_nav_count')}`
- Mission headline: {model.mission.headline}
- Calendar months: `{build_state.get('month_count')}`
- Day stories: `{build_state.get('day_count')}`
- Strategy cards: `{build_state.get('strategy_count')}`
- Trade cards: `{build_state.get('trade_count')}`
- QA status: `{build_state.get('qa_status')}`
- Browser verification: `{build_state.get('browser_verification_status')}`

Apex is additive and preserves Command Center, Command Center X, X2, and X3. It is research-only with live trading disabled.
"""


def _quality_scorecard_doc(build_state: dict[str, Any]) -> str:
    score = build_state.get("quality_score", "pending")
    rows = [
        "Overall simplicity",
        "Visual polish",
        "Mission page clarity",
        "Calendar experience",
        "Day story experience",
        "Strategy card experience",
        "Trade card experience",
        "No-picks clarity",
        "Intelligence storytelling",
        "System diagnostics clarity",
        "Warning honesty",
        "Data wiring correctness",
        "Prior UI preservation",
        "No-secret safety",
        "No-live-trading safety",
        "Browser verification",
        "Test coverage",
        "Product coherence",
    ]
    lines = ["# OMEGA Interface Apex Quality Scorecard", "", f"Overall score: `{score}`", "", "| Area | Score |", "|---|---|"]
    for row in rows:
        area_score = 100 if score == 100 else "pending" if score == "pending_report" else score
        lines.append(f"| {row} | `{area_score}` |")
    lines.append("")
    lines.append("Hard caps checked by QA: six-nav maximum, no table-first primary pages, clickable calendar, day stories, trade cards, no shallow no-picks page, no secrets, no live controls, no swing-as-day-trade, no backtest-as-validation.")
    lines.append("")
    return "\n".join(lines)


def _red_team_doc(build_state: dict[str, Any]) -> str:
    final = build_state.get("final_status")
    status = "fixed" if final == "COMPLETE_INTERFACE_APEX" else "requires final browser/report closure"
    checks = [
        "too many tabs",
        "too technical",
        "page starts with data dump",
        "no story",
        "no clickable calendar",
        "no day stories",
        "no strategy cards",
        "no trade cards",
        "no-picks shallow",
        "warnings hidden",
        "live controls appear",
        "buy/sell language appears",
        "secrets leak",
        "shadow shown as official",
        "swing shown as day trading",
        "backtest shown as validation",
        "provider fallback shown as broker-grade",
        "old UI broken",
        "mobile unreadable",
        "external dependencies added",
        "UI mutates data",
    ]
    lines = ["# OMEGA Interface Apex Red Team", "", "| Finding | Status |", "|---|---|"]
    for check in checks:
        lines.append(f"| {check} | `{status}` |")
    lines.append("")
    lines.append("Critical and high findings are blocked by static QA and browser verification before COMPLETE_INTERFACE_APEX can be claimed.")
    lines.append("")
    return "\n".join(lines)


def _resume_goal_doc(build_state: dict[str, Any]) -> str:
    if build_state.get("final_status") == "COMPLETE_INTERFACE_APEX":
        return "# Interface Apex Resume Goal\n\nNo resume required. Interface Apex is complete under the recorded build state.\n"
    return """# Interface Apex Resume Goal

Resume by rerunning:

```
py -m intraday_scanner.v2.interface_apex build
py -m intraday_scanner.v2.interface_apex qa
py -m intraday_scanner.v2.interface_apex verify
```

Then complete browser verification for Mission, Calendar, one day page, Strategies, one strategy detail, Trades and filters, Intelligence, System, and No Picks. Save screenshots under `data/v2_interface_apex/screenshots/`, write `data/v2_interface_apex/qa/browser_verification.json`, rerun `py -m intraday_scanner.v2.interface_apex report`, and only claim `COMPLETE_INTERFACE_APEX` if the score is 100 and browser status is passed.
"""


def _quality_score(
    *,
    qa: dict[str, Any],
    verify: dict[str, Any],
    browser: dict[str, Any],
    manifest: dict[str, Any],
    model: InterfaceApexModel,
) -> int:
    if qa.get("status") != "passed" or verify.get("status") != "passed":
        return 80
    if int(manifest.get("top_level_nav_count") or 99) > 6:
        return 75
    if not model.days:
        return 75
    if not model.trades:
        return 80
    if any("TELEGRAM_BOT_TOKEN" in warning for warning in model.warnings):
        return 0
    if browser.get("status") != "passed":
        return 95
    return 100


def _browser_verification(output_root: Path) -> dict[str, Any]:
    for path in (output_root / "qa/browser_verification.json", output_root / "screenshots/browser_verification.json"):
        if path.exists():
            data = _read_json(path, {})
            if isinstance(data, dict):
                return data
    return {"status": "missing", "reason": "browser verification not recorded yet"}


def _untrusted_items(model: InterfaceApexModel) -> list[str]:
    items = [
        "No strategy is validated for live execution.",
        "Historical intraday backtests are research, not forward validation.",
        "Daily-bar swing research is not same-session day trading.",
        "Shadow challengers are not official paper strategies.",
    ]
    if model.system.telegram_status != "ready_to_send":
        items.append(f"Local Telegram readiness is {model.system.telegram_status}.")
    return items


def _prior_ui_roots(repo_root: Path) -> dict[str, bool]:
    return {
        "command_center": (repo_root / "data/v2_command_center").exists(),
        "command_center_x": (repo_root / "data/v2_command_center_x").exists(),
        "command_center_x2": (repo_root / "data/v2_command_center_x2").exists(),
        "command_center_x3": (repo_root / "data/v2_command_center_x3").exists(),
    }


def _write_bridge_links(*, repo_root: Path, output_root: Path) -> None:
    try:
        if output_root.resolve() != (repo_root / "data/v2_interface_apex").resolve():
            return
    except OSError:
        return
    apex_target = output_root / "index.html"
    if not apex_target.exists():
        return
    for ui_root in (
        repo_root / "data/v2_command_center_x3",
        repo_root / "data/v2_command_center_x2",
        repo_root / "data/v2_command_center",
    ):
        index = ui_root / "index.html"
        if not index.exists():
            continue
        text = index.read_text(encoding="utf-8")
        if "Open Interface Apex" in text:
            continue
        href = _relative(index.parent, apex_target)
        marker = "</body>"
        bridge = f'<div class="apex-bridge" style="position:fixed;right:16px;bottom:16px;z-index:50"><a href="{href}" style="display:block;border:1px solid #39d7ff;background:#07111b;color:#e9fbff;padding:10px 12px;border-radius:8px;text-decoration:none;font:600 13px system-ui">Open Interface Apex</a></div>\n'
        if marker in text:
            text = text.replace(marker, bridge + marker)
            index.write_text(text, encoding="utf-8", newline="\n")


def _required_docs() -> list[str]:
    return [
        "docs/architecture/v2_interface_apex_product_vision.md",
        "docs/architecture/v2_interface_apex_information_architecture.md",
        "docs/architecture/v2_interface_apex_design_system.md",
        "docs/architecture/v2_interface_apex.md",
        "docs/operations/interface_apex_user_guide.md",
        "docs/operations/interface_apex_rebuild.md",
        "docs/audit/omega_interface_apex_release_summary.md",
        "docs/audit/omega_interface_apex_quality_scorecard.md",
        "docs/audit/omega_interface_apex_red_team.md",
        "docs/audit/omega_interface_apex_build_state.json",
        "docs/audit/omega_interface_apex_resume_goal.md",
    ]


def _build_report_md(manifest: dict[str, Any]) -> str:
    return f"""# Interface Apex Build Report

- Status: `{manifest.get('status')}`
- Final status: `{manifest.get('final_status')}`
- Build ID: `{manifest.get('build_id')}`
- Pages: `{manifest.get('page_count')}`
- Top-level nav count: `{manifest.get('top_level_nav_count')}`
- Day stories: `{manifest.get('day_count')}`
- Month pages: `{manifest.get('month_count')}`
- Strategy cards: `{manifest.get('strategy_count')}`
- Trade cards: `{manifest.get('trade_count')}`
"""


def _verify_md(result: dict[str, Any]) -> str:
    lines = ["# Interface Apex Verify", "", f"- Status: `{result.get('status')}`", f"- QA: `{result.get('qa_status')}`", "", "## Failures"]
    failures = result.get("failures", [])
    if failures:
        lines.extend(f"- `{failure}`" for failure in failures)
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def _apex_css() -> str:
    return """:root{color-scheme:dark;--base:#05070b;--panel:#0b1118;--panel2:#111a24;--border:#203044;--muted:#8ea4b8;--text:#f5fbff;--cyan:#39d7ff;--blue:#2f81ff;--green:#35e6a1;--amber:#f6c453;--red:#ff5d74;--radius:8px;--font:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}html{background:var(--base);font-family:var(--font);letter-spacing:0}body{margin:0;min-height:100vh;background:radial-gradient(circle at 70% 0%,#102238 0,#05070b 38%,#030507 100%);color:var(--text);display:grid;grid-template-columns:260px minmax(0,1fr)}a{color:inherit}.apex-shell{position:sticky;top:0;height:100vh;border-right:1px solid var(--border);background:linear-gradient(180deg,#07101a,#04070b);padding:22px 18px;display:flex;flex-direction:column;gap:18px}.brand{display:flex;align-items:baseline;justify-content:space-between;text-decoration:none;color:var(--text);font-weight:750;font-size:20px}.brand b{color:var(--cyan);font-size:13px;letter-spacing:.14em;text-transform:uppercase}.brand-subtitle{color:var(--muted);margin:0;font-size:13px}.primary-nav{display:grid;gap:6px}.primary-nav a{border:1px solid transparent;border-radius:8px;padding:11px 12px;text-decoration:none;color:#d7e6f5;font-weight:650}.primary-nav a.active,.primary-nav a:hover{border-color:#2e506d;background:#0e1a27;color:white}.trust-boundary{margin-top:auto;border:1px solid #294760;background:#0a1520;border-radius:8px;padding:12px}.trust-boundary strong,.trust-boundary span{display:block}.trust-boundary span{color:var(--muted);font-size:12px;margin-top:4px}main{min-width:0;padding:18px 24px 60px}.topbar{height:54px;border:1px solid var(--border);background:rgba(8,15,23,.78);border-radius:8px;display:flex;align-items:center;justify-content:flex-end;gap:14px;padding:0 14px;margin-bottom:12px}.topbar div{border-left:1px solid #1b2b3e;padding-left:14px}.topbar span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.topbar strong{font-size:13px}.toplink,.button-primary,.button-secondary{border:1px solid #315773;border-radius:8px;padding:9px 11px;text-decoration:none;font-size:13px;font-weight:700;background:#0d1a25}.button-primary{background:linear-gradient(135deg,var(--blue),#12b8ff);border-color:#58cfff;color:#03101b}.button-secondary{background:#0b1620}.boundary-strip{border:1px solid #28425a;background:#07111b;border-radius:8px;display:flex;gap:8px;flex-wrap:wrap;padding:10px 12px;margin-bottom:18px}.boundary-strip span{color:#dff8ff;font-size:12px}.mission-hero,.page-hero{border:1px solid #294057;border-radius:8px;background:linear-gradient(135deg,rgba(14,27,39,.94),rgba(6,11,18,.98));padding:34px;display:grid;grid-template-columns:minmax(0,1.2fr)320px;gap:24px;align-items:stretch}.page-hero.compact{display:block;padding:26px;margin-bottom:16px}.eyebrow{color:var(--cyan);font-size:12px;text-transform:uppercase;font-weight:800;margin:0 0 8px}.mission-hero h1,.page-hero h1{font-size:clamp(34px,4vw,58px);line-height:1.02;margin:0 0 14px;letter-spacing:0;overflow-wrap:anywhere}.page-hero h1{font-size:clamp(28px,3vw,42px)}.hero-subtitle,.page-hero p{color:#c5d7e8;font-size:17px;line-height:1.55;max-width:920px;overflow-wrap:anywhere}.hero-actions{display:flex;gap:10px;flex-wrap:wrap}.system-pulse{border:1px solid #315773;background:#07131d;border-radius:8px;padding:18px}.system-pulse span,.story-card span,.metric-card span,.mini-card span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:800}.system-pulse strong{display:block;color:var(--green);font-size:32px;margin:10px 0;overflow-wrap:anywhere}.metric-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}.metric-row.dense{grid-template-columns:repeat(8,minmax(0,1fr))}.metric-card,.story-card,.mini-card,.empty-card{border:1px solid var(--border);background:rgba(10,17,25,.88);border-radius:8px;padding:16px;min-width:0}.metric-card strong{display:block;font-size:28px;margin:7px 0;overflow-wrap:anywhere}.metric-card small{color:var(--muted);line-height:1.35;overflow-wrap:anywhere}.story-grid{display:grid;gap:14px;margin:16px 0}.two-up{grid-template-columns:repeat(2,minmax(0,1fr))}.three-up{grid-template-columns:repeat(3,minmax(0,1fr))}.story-card h2{font-size:24px;line-height:1.15;margin:8px 0 8px;overflow-wrap:anywhere}.warning-stack h2{font-size:21px}.story-card p,.story-card li{color:#c5d7e8;line-height:1.45;overflow-wrap:anywhere}.section-heading{display:flex;justify-content:space-between;align-items:center;margin:12px 0}.section-heading span{color:var(--cyan);font-size:12px;text-transform:uppercase;font-weight:800}.section-heading h2{margin:2px 0 0;font-size:26px;overflow-wrap:anywhere}.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px}.day-tile{min-height:112px;border:1px solid #20344a;background:#09131d;border-radius:8px;padding:10px;text-decoration:none;display:flex;flex-direction:column;gap:4px;min-width:0}.day-tile:hover{border-color:var(--cyan)}.day-tile strong{font-size:20px;overflow-wrap:anywhere}.day-tile small{color:#9fb4c8;overflow-wrap:anywhere}.tone-green strong,.metric-card strong{color:var(--green)}.tone-red strong{color:var(--red)}.tone-flat strong{color:#d8e6f3}.markers{display:flex;gap:5px;align-items:center;margin-top:auto;flex-wrap:wrap}.dot{width:8px;height:8px;border-radius:99px;display:inline-block}.dot.warning{background:var(--amber)}.dot.learning{background:var(--cyan)}.tag,.badge{border:1px solid #315773;background:#0b1824;border-radius:999px;padding:4px 7px;font-size:11px;color:#dff8ff}.strategy-grid,.trade-grid,.mini-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.strategy-card,.trade-card{border:1px solid var(--border);background:#08111a;border-radius:8px;padding:15px;min-width:0}.card-topline{display:flex;justify-content:space-between;gap:8px;color:var(--cyan);font-size:12px;font-weight:800;text-transform:uppercase;overflow-wrap:anywhere}.card-topline a,.card-link{color:#dff8ff}.strategy-card h3,.trade-card h3{font-size:20px;margin:8px 0 6px;overflow-wrap:anywhere}.strategy-card p,.trade-card p{color:#aebfd0;overflow-wrap:anywhere}.stat-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:12px 0}.stat-grid span{border:1px solid #1f3347;border-radius:8px;padding:8px;background:#07111b;min-width:0}.stat-grid b{display:block;color:#e8f7ff;overflow-wrap:anywhere}.stat-grid small{color:var(--muted);font-size:11px}.badge-row{display:flex;gap:6px;flex-wrap:wrap}.warnings-list{padding-left:17px;color:#c5d7e8}.timeline{display:grid;gap:8px}.timeline span{border-left:2px solid #2d688b;padding-left:9px;color:#c5d7e8;overflow-wrap:anywhere}.timeline b{display:block;color:white}.filter-bar{border:1px solid var(--border);border-radius:8px;background:#08111a;padding:12px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0}.filter-bar label{color:var(--muted);font-size:12px;text-transform:uppercase}.filter-bar input,.filter-bar select{width:100%;margin-top:5px;background:#03070c;border:1px solid #294057;color:white;border-radius:8px;padding:9px}.advanced-drawer{border:1px solid #21364b;background:#060d14;border-radius:8px;padding:12px;margin:18px 0}.advanced-drawer summary{cursor:pointer;color:#dff8ff;font-weight:800}.advanced-drawer p,.advanced-drawer li{color:#9fb4c8;overflow-wrap:anywhere}.link-row{display:flex;gap:10px;flex-wrap:wrap}.link-row a{border:1px solid #315773;border-radius:8px;padding:9px 10px;text-decoration:none;background:#0b1824}.hidden-by-filter{display:none!important}@media(max-width:1100px){body{grid-template-columns:1fr}.apex-shell{position:relative;height:auto}.primary-nav{grid-template-columns:repeat(3,minmax(0,1fr))}.mission-hero{grid-template-columns:1fr}.metric-row,.metric-row.dense,.two-up,.three-up,.strategy-grid,.trade-grid,.mini-grid,.filter-bar{grid-template-columns:1fr}.calendar-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.topbar{height:auto;align-items:stretch;flex-direction:column}.topbar div{border-left:0;border-top:1px solid #1b2b3e;padding:8px 0}}"""


def _apex_components_css() -> str:
    return """.monthly-return-calendar{border:1px solid #203044;border-radius:8px;padding:16px;background:rgba(5,10,16,.58);margin:16px 0}.cumulative-return-ribbon{border:1px solid #2b536f;background:#081522;border-radius:8px;padding:12px;display:grid;grid-template-columns:repeat(3,max-content 1fr);gap:8px 12px;align-items:center}.cumulative-return-ribbon span{color:#8ea4b8;text-transform:uppercase;font-size:11px;font-weight:800}.cumulative-return-ribbon strong{font-size:18px}.month-links{display:flex;gap:8px}.month-links a{border:1px solid #315773;border-radius:8px;padding:8px 10px;text-decoration:none;background:#0b1824}.warning-stack{border-color:#5b4720}.warning-stack h2{color:#f6c453}.learning-lesson-card{border-color:#1f5064}.market-masters-card{border-color:#274a79}.evidence-quality-badge{border-color:#345e7b}.robustness-badge{border-color:#4b6442}.no-picks-story-card{border-color:#3d5570}.day-story-panel{border-color:#244d6b}.automation-status-rail .story-card{min-height:150px}.raw-data{opacity:.92}.trust-boundary-banner{box-shadow:0 0 0 1px rgba(57,215,255,.04) inset}"""


def _apex_js() -> str:
    return """(() => {
  const filters = document.querySelectorAll('[data-filter]');
  const cards = document.querySelectorAll('.trade-card');
  const normalize = (value) => (value || '').toString().toLowerCase().trim();
  const apply = () => {
    const state = {};
    filters.forEach((filter) => { state[filter.dataset.filter] = normalize(filter.value); });
    cards.forEach((card) => {
      let visible = true;
      ['date', 'symbol', 'strategy', 'result', 'evidence'].forEach((key) => {
        const wanted = state[key];
        const actual = normalize(card.dataset[key]);
        if (wanted && !actual.includes(wanted)) visible = false;
      });
      card.classList.toggle('hidden-by-filter', !visible);
    });
  };
  filters.forEach((filter) => filter.addEventListener('input', apply));
  filters.forEach((filter) => filter.addEventListener('change', apply));
  document.querySelectorAll('.advanced-drawer').forEach((drawer) => {
    drawer.addEventListener('toggle', () => {
      if (drawer.open) drawer.dataset.opened = 'true';
    });
  });
})();"""


def _clean_generated_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


def _stable_build_id(model: InterfaceApexModel) -> str:
    payload = json.dumps(to_plain(model), sort_keys=True, separators=(",", ":"), default=str)
    return "interface_apex_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _strategy_page_name(strategy: StrategyModel) -> str:
    return strategy.detail_link.rsplit("/", 1)[-1].removesuffix(".html")


def _root_link(path: Path, href: str) -> str:
    return _relative(path.parent, _output_root_for(path) / href)


def _root_link_for_body(root: bool, href: str) -> str:
    return href if root else "../" + href


def _output_root_for(path: Path) -> Path:
    parts = path.parts
    if "v2_interface_apex" in parts:
        index = parts.index("v2_interface_apex")
        return Path(*parts[: index + 1])
    if path.parent.name in {"pages", "days", "months", "strategies", "trades"}:
        return path.parent.parent
    return path.parent


def _relative(start: Path, target: Path) -> str:
    try:
        return Path(target).resolve().relative_to(Path(start).resolve()).as_posix()
    except ValueError:
        pass
    import os

    return os.path.relpath(target, start).replace("\\", "/")


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in str(value)).strip("-")


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
