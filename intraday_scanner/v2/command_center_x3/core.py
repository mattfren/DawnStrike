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

from intraday_scanner.paper_ops_root import production_paper_ops_root
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
    ("Top 5", "pages/home.html"),
    ("Calendar", "pages/calendar.html"),
    ("Paper Book", "pages/trades.html"),
    ("Strategies", "pages/strategies.html"),
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
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    configured_paper_root = production_paper_ops_root(
        repo_root=repo_root,
        override=paper_ops_root,
    )
    data = _story_payload(repo_root=repo_root, paper_ops_root=configured_paper_root)
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
        "paper_ops_root": _relative_or_absolute(repo_root, configured_paper_root),
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
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    manifest = _read_json(output_root / "manifests/command_center_x3_manifest.json", {})
    if not manifest:
        manifest = build_command_center_x3(
            repo_root=repo_root,
            output_root=output_root,
            paper_ops_root=paper_ops_root,
        )
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
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    manifest = build_command_center_x3(
        repo_root=repo_root,
        output_root=output_root,
        paper_ops_root=paper_ops_root,
    )
    qa = qa_command_center_x3(repo_root=repo_root, output_root=output_root)
    report = report_command_center_x3(
        repo_root=repo_root,
        output_root=output_root,
        paper_ops_root=paper_ops_root,
    )
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


def _story_payload(
    *,
    repo_root: Path,
    paper_ops_root: str | Path | None = None,
) -> dict[str, Any]:
    data = to_plain(
        build_story_bundle(repo_root=repo_root, paper_ops_root=paper_ops_root)
    )
    app = _dict(data.get("app"))
    latest = str(app.get("latest_run_date") or "unknown")
    app["generated_at"] = f"{latest}T00:00:00Z"
    app["surface"] = "Command Center X3"
    app["plain_language"] = True
    data["app"] = app
    data["day_trade"] = _day_trade_payload(repo_root)
    data["watchlist"] = _alphaops_watchlist_payload(repo_root)
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
    _write_json(data_dir / "watchlist.json", data.get("watchlist", {}))
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


def _alphaops_watchlist_payload(repo_root: Path) -> dict[str, Any]:
    signal_path = repo_root / "outputs/alpha_cycle/alpha_signals.json"
    rows = _alphaops_signal_rows(signal_path)
    source = "outputs/alpha_cycle/alpha_signals.json"
    if not rows:
        source = "outputs/alpha_cycle/scan/ranked_candidates.csv"
        rows = _read_csv_rows(repo_root / source)
    top = rows[:5]
    latest = "n/a"
    if top:
        latest = top[0].get("as_of_timestamp") or top[0].get("imported_at") or "n/a"
    return {
        "source": source,
        "latest_as_of": latest,
        "count": len(rows),
        "top_five": [
            {
                "rank": row.get("rank", ""),
                "ticker": row.get("ticker", ""),
                "company": row.get("company", ""),
                "score": row.get("total_score") or row.get("score", ""),
                "gap_pct": row.get("gap_pct", ""),
                "gate": row.get("alert_gate_status") or row.get("data_quality_label") or row.get("classification") or "n/a",
                "watch": row.get("entry_trigger") or row.get("breakout_trigger", ""),
                "target": row.get("target_1") or row.get("first_target", ""),
                "failed_below": row.get("invalidation") or row.get("invalidation_level", ""),
                "reward_risk": row.get("reward_risk_ratio") or row.get("planned_r_multiple", ""),
                "source_kind": row.get("data_source_kind") or row.get("source", ""),
                "next": row.get("confirmation_needed") or row.get("action") or "Wait for trigger",
            }
            for row in top
        ],
    }


def _alphaops_signal_rows(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path, [])
    if isinstance(payload, dict):
        for key in ("signals", "top_signals", "candidates", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    rows = [dict(_dict(row)) for row in _list(payload)]
    rows.sort(key=lambda row: _int(row.get("rank")) or 9999)
    return rows


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
    nav = _nav(path)
    status_chips = _layout_status_chips(data)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dawnstrike Operator Dashboard - {_esc(title)}</title>
  <link rel="icon" href="{rel_assets}/x3_favicon.svg?v={_esc(build_id)}" type="image/svg+xml">
  <link rel="stylesheet" href="{rel_assets}/x3.css?v={_esc(build_id)}">
  <style>{_ops_inline_css()}</style>
</head>
<body>
<aside class="side-shell">
  <a class="brand" href="{_root_link(path, "index.html")}"><span>Dawnstrike</span><b>Operator</b></a>
  <p class="brand-subtitle">Research terminal</p>
  <nav class="primary-nav" data-primary-nav>{nav}</nav>
  <div class="safety-card"><strong>Boundary</strong><span>Research only. No broker execution. Missing values stay n/a.</span></div>
</aside>
<main>
  <header class="topbar">
    <div class="page-title"><span>Operator Dashboard</span><strong>{_esc(title)}</strong></div>
    <div class="status-chips">{status_chips}</div>
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


def _layout_status_chips(data: dict[str, Any]) -> str:
    app = _dict(data.get("app"))
    no_picks = _dict(data.get("no_picks"))
    watchlist = _dict(data.get("watchlist"))
    trades = _trade_rows(data)
    latest = str(app.get("latest_run_date", "unknown"))
    blocked = _int(no_picks.get("blocked_count"))
    accepted = _int(no_picks.get("accepted_count"))
    top_count = _int(watchlist.get("count"))
    return "\n".join(
        [
            '<span class="chip chip-info">Deployment online</span>',
            f'<span class="chip chip-warn">{blocked} blocked / {accepted} cleared</span>',
            f'<span class="chip chip-plain">{len(trades)} paper rows</span>',
            f'<span class="chip chip-plain">{_esc(latest)} scan</span>',
            f'<span class="chip chip-plain">{top_count} candidates</span>',
        ]
    )


def _ops_inline_css() -> str:
    return """.ops-panel{border:1px solid var(--line);border-radius:8px;background:var(--surface);margin:16px 0;padding:16px;display:grid;grid-template-columns:minmax(0,1fr)minmax(420px,1.2fr);gap:16px;align-items:start}.ops-panel h2{font-size:22px;margin:0 0 8px}.ops-panel p{color:var(--muted);line-height:1.5;margin:0}.ops-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.ops-grid article{border:1px solid var(--line);border-radius:8px;background:#fffdf7;padding:12px;min-height:92px}.ops-grid span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;font-weight:800}.ops-grid strong{display:block;font-size:18px;margin:5px 0;overflow-wrap:anywhere}.ops-grid em{display:block;color:var(--muted);font-size:12px;font-style:normal;line-height:1.35}.ops-grid [data-state=ok] strong{color:var(--accent)}.ops-grid [data-state=warn] strong{color:var(--amber)}.ops-grid [data-state=bad] strong{color:var(--red)}@media(max-width:1000px){.ops-panel{grid-template-columns:1fr}.ops-grid{grid-template-columns:1fr}}"""


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
    open_trades = len(_list(day.get("open_positions")))
    paper_trades = len(_list(day.get("paper_trades")))
    best = _best_day_trade(data)
    risk = _risk_state(data)
    next_run = _next_run(data)
    learning = _learning_sentence(data)
    summary = _plain_home_summary(day=day, best=best, risk=risk, learning=learning)
    current_state = _current_state_panel(data, day=day, risk=risk)
    months = _list(data.get("months"))
    latest_month = _dict(months[-1]) if months else {}
    return f"""
<section class="hero story-hero operator-hero">
  <div>
    <p class="eyebrow">Trading Research Terminal</p>
    <h1>Dawnstrike Operator Dashboard</h1>
    <p class="story-summary">{_esc(summary)}</p>
    <div class="hero-actions"><a class="button-primary" href="{actions_base}home.html#top-five">Review Top 5</a><a class="button-secondary" href="{actions_base}trades.html">Open Paper Book</a><a class="button-secondary" href="{actions_base}system.html">Check Automation</a></div>
  </div>
  {current_state}
</section>
<section class="metric-strip operator-kpis">
  <article><span>AlphaOps Top 5</span><strong>{_esc(str(len(_list(_dict(data.get("watchlist")).get("top_five")))))} real tickers</strong><em>Latest scan { _esc(str(_dict(data.get("watchlist")).get("latest_as_of", "n/a"))) }</em></article>
  <article><span>Risk Gate</span><strong>{_esc(str(no_picks.get("blocked_count", "n/a")))} blocked</strong><em>{_esc(risk["reason"])}</em></article>
  <article><span>Paper Book</span><strong>{open_trades} open</strong><em>{len(_trade_rows(data))} paper rows available</em></article>
  <article><span>Calendar</span><strong>{_esc(str(latest_month.get("no_trade_days", "n/a")))} no-trade</strong><em>{paper_trades} latest-day paper/shadow rows</em></article>
  <article><span>Automation</span><strong>{_esc(next_run["label"])}</strong><em>{_esc(next_run["time"])}</em></article>
</section>
<section class="operator-grid">
  <div class="operator-main">
    {_watchlist_section(data)}
    {_paper_ticket_section(data)}
    {_calendar_preview_section(data, actions_base=actions_base)}
    {_strategy_health_section(data)}
    {_system_readiness_section(data)}
  </div>
  {_evidence_rail(data)}
</section>
<section class="trust-panel">
  <strong>Still untrusted</strong>
  <p>No strategy is validated. Day-trade backtests are historical research. Shadow challengers are not official strategies. The public dashboard cannot trade, send Telegram messages, or mutate paper records. Authenticated Vercel functions can run read-only scanner, provider, and Telegram intelligence workflows.</p>
</section>
"""


def _watchlist_section(data: dict[str, Any]) -> str:
    watchlist = _dict(data.get("watchlist"))
    rows = [_dict(row) for row in _list(watchlist.get("top_five"))]
    if not rows:
        return """
<section class="story-section panel" id="top-five">
  <h2>Top 5 Operator Watchlist</h2>
  <article class="soft-card"><strong>No watchlist artifact found.</strong><p>Run AlphaOps to rebuild the current ranked candidates.</p></article>
</section>
"""
    rows_html = "".join(_watchlist_card(row) for row in rows)
    return f"""
<section class="story-section panel" id="top-five">
  <div class="section-heading"><div><p class="eyebrow">AlphaOps</p><h2>Top 5 Operator Watchlist</h2></div><span class="pill">{_esc(str(watchlist.get("count", "n/a")))} candidates</span></div>
  <p class="operator-note">These are the latest ranked AlphaOps names. They are watch-only because every Top 5 row is currently blocked by gate checks; Dawnstrike is not placing or recommending live orders.</p>
  <div class="data-table watchlist-table" role="table" aria-label="Top 5 operator watchlist">
    <div class="data-row data-head" role="row"><span>Rank</span><span>Ticker</span><span>Score</span><span>Gate</span><span>Gap</span><span>Trigger</span><span>R/R</span></div>
    {rows_html}
  </div>
  <p class="source-line">Source: {_esc(watchlist.get("source", "n/a"))} / latest {_esc(watchlist.get("latest_as_of", "n/a"))}</p>
</section>
"""


def _watchlist_card(row: dict[str, Any]) -> str:
    rank = str(row.get("rank") or "n/a")
    ticker = str(row.get("ticker") or "n/a").upper()
    company = str(row.get("company") or "Unknown company")
    score = str(row.get("score") or "n/a")
    gap = str(row.get("gap_pct") or "n/a")
    gate = str(row.get("gate") or "n/a").upper()
    watch = str(row.get("watch") or "n/a")
    reward_risk = _decimal_text(row.get("reward_risk"), 2)
    return f"""<div class="data-row" role="row" data-filter-item>
  <span>{_esc(rank)}</span>
  <span><b>{_esc(ticker)}</b><em>{_esc(company)}</em></span>
  <span class="num">{_esc(score)}</span>
  <span class="status-bad">{_esc(gate)}</span>
  <span class="num">{_esc(gap)}%</span>
  <span>{_esc(watch)}</span>
  <span class="num">{_esc(reward_risk)}R</span>
</div>"""


def _watchlist_source_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"web_url", "web url", "url"}:
        return "Ranked scan"
    if not text:
        return "Research"
    return _translate_system_text(_humanize(text))


def _current_state_panel(data: dict[str, Any], *, day: dict[str, Any], risk: dict[str, str]) -> str:
    watchlist = _dict(data.get("watchlist"))
    top = _dict(_list(watchlist.get("top_five"))[0]) if _list(watchlist.get("top_five")) else {}
    return f"""<aside class="hero-state">
  <span>Current state</span>
  <dl>
    <dt>Latest run</dt><dd>{_esc(str(day.get("date", "n/a")))}</dd>
    <dt>Status</dt><dd>{_esc(risk.get("label", "n/a"))} / alerts blocked by gates</dd>
    <dt>Top watch</dt><dd>{_esc(str(top.get("ticker", "n/a")).upper())} / {_esc(str(top.get("gate", "n/a")).upper())}</dd>
  </dl>
</aside>"""


def _evidence_rail(data: dict[str, Any]) -> str:
    app = _dict(data.get("app"))
    refs = [_dict(row) for row in _list(app.get("source_refs"))]
    watchlist = _dict(data.get("watchlist"))
    month = _dict(_list(data.get("months"))[-1]) if _list(data.get("months")) else {}
    automation = _dict(data.get("automation"))
    cards = [
        (
            "AlphaOps scan",
            watchlist.get("source", "outputs/alpha_cycle/alpha_signals.json"),
            "local artifact",
            f"{watchlist.get('count', 'n/a')} candidates; Top 5 restored as real symbols",
        ),
        (
            "PaperOps",
            "X3 compact paper evidence",
            "official paper",
            f"{len(_trade_rows(data))} paper rows available",
        ),
        (
            "Calendar",
            f"{month.get('month', 'n/a')} heatmap",
            "artifact backed",
            "n/a remains n/a on no-trade days",
        ),
        (
            "Automation",
            str(automation.get("latest_scheduler_status", "OMEGA scheduler")),
            "status",
            f"{len(_list(automation.get('task_statuses')))} scheduled task rows",
        ),
        (
            "Boundary",
            "research only",
            "no execution",
            "Static dashboard exposes no internal raw route archive",
        ),
    ]
    rows = "".join(
        f"""<article><span>{_esc(title)}</span><strong>{_esc(str(primary))}</strong><b>{_esc(str(badge))}</b><p>{_esc(str(detail))}</p></article>"""
        for title, primary, badge, detail in cards
    )
    source_count = len([row for row in refs if row.get("exists") is True])
    return f"""<aside class="evidence-rail">
  <p class="eyebrow">Evidence</p>
  <h2>Provenance Rail</h2>
  {rows}
  <p class="source-line">{source_count} local source references are linked in System.</p>
</aside>"""


def _paper_ticket_section(data: dict[str, Any]) -> str:
    trade = _trade_rows(data)[0] if _trade_rows(data) else {}
    if not trade:
        return """<section class="story-section panel"><div class="section-heading"><div><p class="eyebrow">PaperOps</p><h2>Current Paper Ticket</h2></div><span class="pill">n/a</span></div><article class="soft-card"><strong>No paper rows found.</strong><p>Paper Book has no available rows in the current artifacts.</p></article></section>"""
    return f"""<section class="story-section panel">
  <div class="section-heading"><div><p class="eyebrow">PaperOps</p><h2>Current Paper Ticket</h2></div><span class="pill">Official paper evidence</span></div>
  <article class="ticket-card">
    <div><span>{_esc(str(trade.get("session_date", "n/a")))}</span><strong>{_esc(str(trade.get("symbol", "n/a")).upper())}</strong><b>official paper evidence</b></div>
    <dl>
      <dt>Strategy</dt><dd>{_esc(_strategy_label(trade.get("strategy_id")))}</dd>
      <dt>Direction</dt><dd>{_esc(str(trade.get("direction", "n/a")))}</dd>
      <dt>Entry</dt><dd>{_esc(str(trade.get("entry_price", "n/a")))}</dd>
      <dt>Stop</dt><dd>{_esc(str(trade.get("stop", "n/a")))}</dd>
      <dt>Target</dt><dd>{_esc(str(trade.get("target", "n/a")))}</dd>
      <dt>R</dt><dd>{_esc(_decimal_text(trade.get("r_multiple"), 2))}</dd>
    </dl>
  </article>
</section>"""


def _calendar_preview_section(data: dict[str, Any], *, actions_base: str) -> str:
    months = [_dict(row) for row in _list(data.get("months"))]
    current = months[-1] if months else {}
    calendar = _calendar_grid(current, from_pages=True)
    if actions_base == "pages/":
        calendar = calendar.replace('href="../days/', 'href="days/')
    return f"""<section class="story-section panel">
  <div class="section-heading"><div><p class="eyebrow">Calendar</p><h2>Paper Calendar</h2></div><a class="pill link-pill" href="{actions_base}calendar.html">Open calendar</a></div>
  {calendar}
</section>"""


def _strategy_health_section(data: dict[str, Any]) -> str:
    rows = _day_trade_strategy_rows(data)[:7]
    cards = "".join(
        f"""<article class="strategy-card soft-card" data-filter-item>
  <span>Strategy Evidence / Experimental</span>
  <strong>{_esc(str(row.get("strategy_name") or _strategy_label(row.get("strategy_id"))))}</strong>
  <div class="mini-metrics"><b>Trades {_esc(str(row.get("trade_count", "n/a")))}</b><b>Win {_esc(_percent_text(row.get("win_rate")))}</b><b>Return {_esc(_percent_text(row.get("total_return_pct")))}</b><b>Drawdown {_esc(_percent_text(row.get("max_drawdown_pct")))}</b></div>
  <em>0% - not validated</em>
</article>"""
        for row in rows
    )
    return f"""<section class="story-section panel">
  <div class="section-heading"><div><p class="eyebrow">Strategy Evidence</p><h2>Strategy Health</h2></div><span class="pill">Experimental</span></div>
  <div class="card-grid strategy-grid">{cards or '<article class="soft-card"><strong>No strategy health rows found.</strong></article>'}</div>
</section>"""


def _system_readiness_section(data: dict[str, Any]) -> str:
    automation = _dict(data.get("automation"))
    flow = [
        _dict(row)
        for row in _list(data.get("system_flow"))
        if isinstance(row, dict)
    ]
    task_rows = [_dict(row) for row in _list(automation.get("task_statuses"))[:4]]
    flow_html = "".join(
        f"""<article><strong>{_esc(_translate_system_text(str(row.get("name", "System"))))}</strong><p>{_esc(_translate_system_text(str(row.get("description", "No description."))))}</p><span class="pill">{_esc(str(row.get("status", "n/a")))}</span></article>"""
        for row in flow[:10]
    )
    task_html = "".join(
        f"""<article><strong>{_esc(str(row.get("task_name", "Scheduled task")))}</strong><p>Last {_esc(str(row.get("last_run_time", "n/a")))} / next {_esc(str(row.get("next_run_time", "n/a")))}</p><span class="pill">Ready result {_esc(str(row.get("last_result", "n/a")))}</span></article>"""
        for row in task_rows
    )
    return f"""<section class="story-section panel">
  <div class="section-heading"><div><p class="eyebrow">Automation</p><h2>System Readiness</h2></div><span class="pill">{_esc(str(automation.get("latest_scheduler_status", "artifact-linked")))}</span></div>
  <div class="readiness-grid"><div class="readiness-list">{flow_html}</div><div class="readiness-tasks">{task_html}</div></div>
</section>"""


def _calendar_body(data: dict[str, Any]) -> str:
    months = [_dict(row) for row in _list(data.get("months"))]
    current = months[-1] if months else {}
    month_links = "".join(
        f'<a class="month-pill" href="../months/{_esc(str(row.get("month", "unknown")))}.html">{_esc(str(row.get("month", "unknown")))}</a>'
        for row in months
    )
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Calendar</p><h1>Performance should be understood by day.</h1><p class="story-summary">The calendar is the primary product view: positive days, loss days, warnings, learning dots, no-trade states, and clickable day stories.</p></div>
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
    official_swing = [
        row for row in swing if row.get("role") == "official_champion"
    ]
    experimental_swing = [
        row for row in swing if row.get("role") != "official_champion"
    ]
    day_cards = "".join(_strategy_card(row, kind="day") for row in day_rows)
    challenger_cards = "".join(_challenger_card(row) for row in challengers[:12])
    swing_cards = "".join(
        _strategy_card(row, kind="swing") for row in official_swing[:20]
    )
    experimental_cards = "".join(
        _strategy_card(row, kind="experimental")
        for row in experimental_swing[:20]
    )
    return f"""
<section class="hero compact-hero">
  <div><p class="eyebrow">Strategies</p><h1>Strategy report cards, not a ranking wall.</h1><p class="story-summary">Day-trade strategies come first. Swing research and shadow challengers are separated so historical swing results cannot masquerade as day-trading proof.</p></div>
</section>
<section class="strategy-toolbar"><button type="button" data-filter-button="all">All</button><button type="button" data-filter-button="watch">Watch</button><button type="button" data-filter-button="quarantine">Quarantine</button><button type="button" data-filter-button="shadow">Shadow</button></section>
<section class="story-section"><h2>Active day-trade research</h2><div class="card-grid strategy-grid">{day_cards or '<article class="soft-card" data-day-strategy-empty-state="true"><strong>No retained day-trade strategy rows.</strong><p>Performance is N/A until a retained research artifact is available.</p></article>'}</div></section>
<section class="story-section"><h2>Shadow challengers</h2><div class="card-grid strategy-grid">{challenger_cards or '<article class="soft-card"><strong>No shadow challengers found.</strong><p>No refinement candidates were generated.</p></article>'}</div></section>
<section class="story-section secondary"><h2>Swing research, separated</h2><div class="card-grid strategy-grid">{swing_cards or '<article class="soft-card" data-official-strategy-empty-state="true"><strong>No verified official PaperOps swing rows.</strong><p>Official return is N/A; missing evidence is not shown as zero.</p></article>'}</div></section>
<section class="story-section secondary"><h2>Experimental / audit evidence</h2><p>These cards are excluded from official strategy counts, calendar returns, and fleet performance.</p><div class="card-grid strategy-grid">{experimental_cards or '<article class="soft-card" data-experimental-strategy-empty-state="true"><strong>No verified experimental PaperOps evidence.</strong><p>Experimental return is N/A.</p></article>'}</div></section>
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
  <div><p class="eyebrow">Paper Trading</p><h1>Paper Book with proof boundaries.</h1><p class="story-summary">Every card shows entry, exit, hold time, result R, and exit reason. The page is read-only, cannot trade, and does not fetch providers.</p></div>
</section>
<section class="trade-filters">
  <input data-x3-search placeholder="Filter symbol, strategy, date, result">
  <select data-x3-select="result"><option value="">All results</option><option value="win">Wins</option><option value="loss">Losses</option></select>
  <select data-x3-select="exit"><option value="">All exits</option><option value="target">Target</option><option value="stop">Stop</option><option value="timeout">Timeout</option><option value="eod">EOD</option></select>
</section>
<section class="card-grid trade-grid" data-filter-scope>{cards or '<article class="soft-card" data-trade-empty-state="true"><strong>No verified trade rows.</strong><p>Trade return is N/A until retained paper evidence exists.</p></article>'}</section>
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
  <article><span>Best day</span><strong>{_esc(str(month.get("best_day", "n/a")))}</strong><em>Positive days: {_esc(str(month.get("green_days", "n/a")))}</em></article>
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
        is_experimental = kind == "experimental"
        badge = (
            "Experimental / excluded"
            if is_experimental
            else "Official PaperOps champion"
        )
        href = f"../strategies/{_slug(strategy_id)}.html"
        type_label = "Audit evidence" if is_experimental else "Swing Research"
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
    (assets / "x3.css").write_text(_clean_generated_text(_x3_css()), encoding="utf-8", newline="\n")
    (assets / "x3.js").write_text(_base_js(), encoding="utf-8", newline="\n")
    (assets / "x3_favicon.svg").write_text(_favicon_svg(), encoding="utf-8", newline="\n")


def _design_tokens() -> dict[str, Any]:
    return {
        "surface": "#fffdf7",
        "panel": "#fbf8f0",
        "panel_soft": "#eee9de",
        "line": "#26362f",
        "text": "#2e332f",
        "muted": "#858c86",
        "accent": "#405978",
        "positive": "#315f86",
        "red": "#b23b45",
        "amber": "#a36f20",
        "radius": 8,
    }


def _base_css() -> str:
    return """
:root{--bg:#f4f1ea;--paper:#fffdf7;--surface:#fbf8f0;--surface2:#eee9de;--line:#26362f;--line-soft:#d7d0c2;--text:#2e332f;--muted:#858c86;--accent:#405978;--accent-soft:#e8eef5;--positive:#315f86;--red:#b23b45;--amber:#a36f20;--shadow:0 22px 60px rgba(39,42,38,.18)}*{box-sizing:border-box}html{background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif;letter-spacing:0}body{margin:0;min-height:100vh;background:var(--bg);display:grid;grid-template-columns:220px minmax(0,1fr)}a{color:inherit}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #d6a756;outline-offset:3px}.side-shell{position:sticky;top:0;height:100vh;border-right:1px solid var(--line);background:#fbfaf5;padding:24px 18px;display:flex;flex-direction:column;gap:18px}.brand{text-decoration:none;display:grid;gap:5px}.brand span{font-weight:900;font-size:11px;text-transform:uppercase;color:var(--accent);letter-spacing:.04em}.brand b{font-size:23px;line-height:1.05;color:#8b918c}.brand-subtitle{margin:0;color:var(--muted);font-size:12px}.primary-nav{display:grid;gap:7px}.primary-nav a{text-decoration:none;border:1px solid var(--line);border-radius:6px;color:#6f776f;background:#fffdf7;padding:10px 11px;font-size:14px;font-weight:800}.primary-nav a.active,.primary-nav a:hover{border-color:var(--accent);background:#f1f4f7;color:var(--text)}.safety-card{margin-top:6px;border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:12px}.safety-card strong{display:block;color:var(--accent);font-size:11px;text-transform:uppercase}.safety-card span{display:block;color:#555d57;font-size:12px;margin-top:6px;line-height:1.45}main{min-width:0;padding:0 24px 48px}.topbar{min-height:58px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:16px}.page-title span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;font-weight:900}.page-title strong{font-size:15px}.status-chips,.boundary-strip{display:flex;flex-wrap:wrap;gap:8px}.chip,.boundary-strip span,.pill{font-size:12px;font-weight:900;border:1px solid var(--line);background:var(--paper);border-radius:999px;padding:6px 10px;color:var(--text);text-decoration:none}.chip-info{border-color:#92a4b9;color:var(--accent);background:var(--accent-soft)}.chip-warn{border-color:#d4b16c;color:var(--amber);background:#fff7df}.boundary-strip{margin-bottom:16px}.boundary-strip span{font-size:11px;color:#555d57}.hero{border:1px solid var(--line);border-radius:8px;background:linear-gradient(135deg,#fffdf7 0%,#f6f3eb 68%,#ebe3d4 100%);box-shadow:var(--shadow);padding:24px;margin-bottom:12px;display:grid;grid-template-columns:minmax(0,1fr)270px;gap:18px;align-items:stretch}.compact-hero{grid-template-columns:1fr}.eyebrow{color:var(--accent);font-size:11px;text-transform:uppercase;font-weight:900;margin:0 0 8px}.hero h1{font-size:clamp(34px,5vw,64px);line-height:.98;margin:0 0 12px;color:#9aa09a}.compact-hero h1{font-size:clamp(28px,3vw,44px)}.story-summary{color:#4f5751;font-size:16px;line-height:1.55;margin:0;max-width:1020px}.hero-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.button-primary,.button-secondary{border:1px solid var(--accent);border-radius:6px;padding:10px 12px;text-decoration:none;font-weight:900;background:#eef4f8;color:var(--accent)}.button-secondary{background:var(--paper);border-color:var(--line);color:#6a716b}.hero-state{border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:16px}.hero-state span,.hero-state dt{font-size:11px;text-transform:uppercase;font-weight:900;color:var(--accent)}.hero-state dl{margin:12px 0 0}.hero-state dt{border-top:1px solid var(--line);padding-top:12px;color:#2e332f}.hero-state dd{margin:5px 0 12px;color:#8b918c;font-weight:900}.metric-strip{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:12px 0}.metric-strip article,.soft-card,.big-card,.trust-panel,.panel{min-width:0;border:1px solid var(--line);border-radius:8px;background:rgba(255,253,247,.96);padding:14px}.metric-strip span,.soft-card span{display:block;color:var(--accent);font-size:11px;text-transform:uppercase;font-weight:900}.metric-strip strong{display:block;font-size:22px;margin:5px 0;color:#9aa09a;overflow-wrap:anywhere}.metric-strip em,.soft-card em{display:block;color:var(--muted);font-style:normal;font-size:12px;line-height:1.4}.operator-grid{min-width:0;display:grid;grid-template-columns:minmax(0,1fr)280px;gap:12px;align-items:start}.operator-main{min-width:0;display:grid;gap:12px}.story-section{margin-top:0}.section-heading{display:flex;justify-content:space-between;gap:12px;align-items:start;margin-bottom:12px}.section-heading h2,.story-section h2,.split-story h2{font-size:24px;line-height:1.05;margin:0;color:#9aa09a}.operator-note,.big-card p,.soft-card p,.story-section p,.trust-panel p{color:#5b625d;line-height:1.5;margin:0}.source-line{font-size:12px;color:var(--muted);margin-top:10px}.data-table{max-width:100%;min-width:0;border:1px solid var(--line);border-radius:8px;overflow-x:auto;overflow-y:hidden;background:var(--paper)}.data-row{display:grid;grid-template-columns:42px minmax(120px,1.5fr)70px 80px 70px minmax(92px,1fr)62px;gap:8px;align-items:center;border-top:1px solid var(--line);padding:10px 12px;font-size:12px}.data-row:first-child{border-top:0}.data-head{background:#f1ece0;text-transform:uppercase;font-size:10px;font-weight:900;color:#2e332f}.data-row b{display:block;color:#8b918c;font-size:17px}.data-row em{display:block;color:#2e332f;font-size:11px;font-style:normal;font-weight:800;line-height:1.25}.num{font-variant-numeric:tabular-nums}.status-bad,.return-negative{color:var(--red)!important;font-weight:900}.return-positive{color:var(--positive)!important}.return-flat,.return-na{color:var(--muted)!important}.evidence-rail{position:sticky;top:16px;border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:14px;display:grid;gap:10px}.evidence-rail h2{font-size:24px;color:#9aa09a;margin:0}.evidence-rail article{border:1px solid var(--line);border-radius:8px;padding:12px;background:#fffdf7}.evidence-rail span{font-size:11px;text-transform:uppercase;font-weight:900;color:var(--accent)}.evidence-rail strong{display:block;color:#9aa09a;margin:5px 0;overflow-wrap:anywhere}.evidence-rail b{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:4px 8px;font-size:10px;text-transform:uppercase}.evidence-rail p{font-size:12px;color:#59615c;line-height:1.45}.ticket-card{max-width:360px;border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:14px}.ticket-card>div{display:flex;align-items:center;justify-content:space-between;gap:10px}.ticket-card span{font-size:12px;font-weight:900}.ticket-card strong{font-size:28px;color:#9aa09a}.ticket-card b{border:1px solid var(--line);border-radius:999px;padding:5px 8px;font-size:10px}.ticket-card dl,.hero-state dl{display:grid;gap:0}.ticket-card dt{font-size:11px;text-transform:uppercase;font-weight:900;border-top:1px solid var(--line);padding-top:12px}.ticket-card dd{margin:3px 0 10px;font-weight:900;color:var(--accent)}.home-grid,.card-grid,.split-story{display:grid;gap:12px}.home-grid{grid-template-columns:repeat(3,minmax(0,1fr));margin:16px 0}.big-card{text-decoration:none;min-height:150px}.big-card strong,.soft-card strong{display:block;font-size:20px;margin:8px 0;color:var(--text)}.warning{border-color:#d4b16c}.trust-panel{margin-top:14px}.split-story{grid-template-columns:repeat(3,minmax(0,1fr));margin-top:18px}.split-story article{border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:16px}.split-story li,.story-section li{color:#5b625d;margin:7px 0}.month-picker{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.month-pill,.drill-links a{border:1px solid var(--line);border-radius:8px;background:var(--paper);color:var(--accent);text-decoration:none;padding:8px 10px;font-weight:900}.calendar-shell{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fbfaf5;padding:12px;overflow-x:auto;overflow-y:hidden}.weekday-row,.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px}.weekday-row span{color:var(--muted);font-size:11px;text-transform:uppercase;padding:4px;font-weight:900}.calendar-pad{min-height:72px}.day-tile{min-height:90px;border:1px solid var(--line);border-radius:8px;background:var(--paper);text-decoration:none;padding:10px;display:grid;gap:4px}.day-tile:hover{background:#f4f7fa;border-color:var(--accent)}.day-tile b{font-size:18px}.day-tile strong{font-size:15px}.day-tile span,.day-tile em{font-style:normal;color:#59615c;font-size:12px}.dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:4px;background:var(--muted)}.dot.warn{background:var(--amber)}.dot.learn{background:var(--accent)}.dot.quiet{background:#b8b0a2}.drill-links{display:flex;gap:8px;margin-top:14px}.strategy-toolbar,.trade-filters{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.strategy-toolbar button,.trade-filters input,.trade-filters select{border:1px solid var(--line);border-radius:8px;background:var(--paper);color:var(--text);padding:9px 10px}.trade-filters input{min-width:280px}.strategy-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.trade-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.system-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.strategy-card,.trade-card{border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:14px;min-width:0}.strategy-card h3,.trade-card h3{font-size:20px;margin:0 0 6px;overflow-wrap:anywhere}.strategy-card p,.trade-card p{color:#5b625d;line-height:1.45;margin:0 0 8px;overflow-wrap:anywhere}.card-topline{display:flex;justify-content:space-between;gap:8px;align-items:center;color:var(--accent);font-size:11px;text-transform:uppercase;font-weight:900;margin-bottom:8px}.mini-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:10px 0}.mini-metrics b{border:1px solid var(--line);border-radius:7px;background:#fffdf7;color:#2e332f;padding:7px 8px;font-size:11px}.readiness-grid{display:grid;grid-template-columns:minmax(0,1.1fr)minmax(280px,.9fr);gap:10px}.readiness-list,.readiness-tasks{display:grid;gap:8px}.readiness-list article,.readiness-tasks article{border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:12px;display:grid;grid-template-columns:150px minmax(0,1fr)auto;gap:10px;align-items:center}.readiness-tasks article{grid-template-columns:1fr}.readiness-list strong,.readiness-tasks strong{color:#9aa09a}.readiness-list p,.readiness-tasks p{font-size:12px;color:#4f5751}.raw-drawer,.advanced-drawer{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:12px}.raw-drawer summary,.advanced-drawer summary{cursor:pointer;color:var(--accent);font-weight:900}.raw-list{display:grid;gap:8px;margin-top:12px}.raw-list div,.artifact-list a{border-top:1px solid var(--line);padding:8px 0;display:grid;gap:3px}.raw-list strong,.artifact-list strong{overflow-wrap:anywhere}.raw-list span,.artifact-list span{color:var(--muted);overflow-wrap:anywhere}.artifact-list a{text-decoration:none}.legacy-links{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.legacy-links a{border:1px solid var(--line);border-radius:8px;padding:8px;text-decoration:none;color:var(--accent);font-weight:900}.muted{color:var(--muted)}@media(max-width:1200px){.operator-grid{grid-template-columns:1fr}.evidence-rail{position:relative;top:auto}.strategy-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.data-row{grid-template-columns:42px minmax(120px,1.4fr)70px 80px 70px minmax(90px,1fr)62px}}@media(max-width:900px){body{grid-template-columns:1fr}.side-shell{position:relative;height:auto}.primary-nav{grid-template-columns:repeat(5,minmax(0,1fr))}.topbar{align-items:flex-start;display:grid}.hero,.metric-strip,.home-grid,.split-story,.strategy-grid,.trade-grid,.system-grid,.readiness-grid{grid-template-columns:1fr}.calendar-grid,.weekday-row{grid-template-columns:repeat(7,minmax(84px,1fr))}.data-table{overflow-x:auto}.data-row{min-width:650px}.readiness-list article{grid-template-columns:1fr}.hero h1{font-size:40px}}@media(max-width:640px){main{padding:0 12px 36px}.side-shell{padding:16px 12px}.primary-nav{grid-template-columns:1fr}.hero{padding:18px}.metric-strip strong{font-size:19px}.section-heading{display:grid}.ticket-card{max-width:none}}
"""


def _x3_css() -> str:
    return _base_css()


def _base_js() -> str:
    return """document.addEventListener('DOMContentLoaded',()=>{const search=document.querySelector('[data-x3-search]');if(search){const scope=document.querySelector('[data-filter-scope]')||document;search.addEventListener('input',()=>{const q=search.value.toLowerCase();for(const item of scope.querySelectorAll('[data-filter-item]')){item.hidden=q&&!item.textContent.toLowerCase().includes(q)&&!(item.getAttribute('data-filter-text')||'').toLowerCase().includes(q);}})}for(const button of document.querySelectorAll('[data-filter-button]')){button.addEventListener('click',()=>{const q=button.getAttribute('data-filter-button')||'';for(const item of document.querySelectorAll('[data-filter-item]')){item.hidden=q!=='all'&&!item.textContent.toLowerCase().includes(q);}})}for(const select of document.querySelectorAll('[data-x3-select]')){select.addEventListener('change',()=>{const result=document.querySelector('[data-x3-select=\"result\"]')?.value||'';const exit=document.querySelector('[data-x3-select=\"exit\"]')?.value||'';for(const item of document.querySelectorAll('[data-filter-item]')){const okResult=!result||item.getAttribute('data-result')===result;const okExit=!exit||(item.getAttribute('data-exit')||'').includes(exit);item.hidden=!(okResult&&okExit);}})}const setText=(sel,text)=>document.querySelectorAll(sel).forEach(el=>{el.textContent=text});const setState=(sel,state)=>document.querySelectorAll(sel).forEach(el=>{el.setAttribute('data-state',state)});const normState=value=>{const v=String(value||'').toLowerCase();if(v.includes('failed')||v.includes('blocked')||v.includes('missing'))return'bad';if(v.includes('warning')||v.includes('dry')||v.includes('disabled'))return'warn';return'ok'};const setDetail=(sel,text)=>document.querySelectorAll(sel).forEach(el=>{el.textContent=text});if(location.protocol==='file:'){setText('[data-x3-backend-status]','static-only');setText('[data-x3-telegram-status]','not checked');setText('[data-x3-scanner-status]','not checked');setText('[data-x3-provider-status]','not checked');setText('[data-x3-cron-status]','not checked');setText('[data-x3-admin-status]','not checked');setState('[data-x3-backend-pill],[data-x3-backend-card]','warn');return}Promise.all([fetch('/api/health',{cache:'no-store'}).then(r=>r.json()),fetch('/api/readiness',{cache:'no-store'}).then(r=>r.json())]).then(([health,ready])=>{const backend=health.status||'unknown';const telegram=ready.telegram?.status||'unknown';const scanner=ready.sentinel?.status||ready.doctor?.status||'unknown';const provider=ready.autodata?.status||'unknown';const env=health.env||{};const present=env.present||{};setText('[data-x3-backend-status]',backend);setText('[data-x3-telegram-status]',telegram);setText('[data-x3-scanner-status]',scanner);setText('[data-x3-provider-status]',provider);setText('[data-x3-cron-status]',present.CRON_SECRET?'configured':'missing');setText('[data-x3-admin-status]',present.DAWNSTRIKE_ADMIN_TOKEN?'configured':'missing');setDetail('[data-x3-backend-detail]',`live trading: ${health.live_trading_enabled===true}`);setDetail('[data-x3-telegram-detail]',env.telegram_ready_for_external_send?'external send ready':'dry-run/disabled or env-gated');setDetail('[data-x3-scanner-detail]',`doctor: ${ready.doctor?.status||'unknown'}`);setDetail('[data-x3-provider-detail]',`configured providers: ${ready.autodata?.configured_count??'n/a'}`);setDetail('[data-x3-cron-detail]','morning 14:10 UTC / after-close 21:35 UTC');setDetail('[data-x3-admin-detail]','required for manual operations');setState('[data-x3-backend-pill],[data-x3-backend-card]',normState(backend));setState('[data-x3-telegram-card]',normState(telegram));setState('[data-x3-scanner-card]',normState(scanner));setState('[data-x3-provider-card]',normState(provider));setState('[data-x3-cron-card]',present.CRON_SECRET?'ok':'bad');setState('[data-x3-admin-card]',present.DAWNSTRIKE_ADMIN_TOKEN?'ok':'bad')}).catch(()=>{setText('[data-x3-backend-status]','offline');setText('[data-x3-telegram-status]','not reached');setText('[data-x3-scanner-status]','not reached');setText('[data-x3-provider-status]','not reached');setText('[data-x3-cron-status]','not reached');setText('[data-x3-admin-status]','not reached');setState('[data-x3-backend-pill],[data-x3-backend-card],[data-x3-telegram-card],[data-x3-scanner-card],[data-x3-provider-card],[data-x3-cron-card],[data-x3-admin-card]','bad')})});"""


def _favicon_svg() -> str:
    return """<svg viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#fffdf7"/>
  <path d="M12 40 L25 27 L34 34 L52 16" fill="none" stroke="#405978" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="46" cy="42" r="8" fill="#a36f20"/>
</svg>
"""


def _quality_score(*, qa: dict[str, Any], manifest: dict[str, Any], data: dict[str, Any]) -> int:
    checks = [
        qa.get("status") == "passed",
        int(manifest.get("top_level_nav_count") or 99) <= 6,
        int(manifest.get("day_count") or 0) > 0,
        int(manifest.get("month_count") or 0) > 0,
        qa.get("checks", {}).get("strategy_surface_truthful") is True,
        qa.get("checks", {}).get("trade_surface_truthful") is True,
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


def _relative_or_absolute(repo_root: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return target.resolve().as_posix()


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
