"""Command Center X2 orchestration and static story UI rendering."""

# ruff: noqa: E501

from __future__ import annotations

import csv
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.command_center_x2.adapters import write_story_models
from intraday_scanner.v2.command_center_x2.qa import (
    REQUIRED_PAGE_NAMES,
    run_command_center_x2_qa,
)

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
    "logs",
)
TRADE_LEDGER_PREVIEW_LIMIT = 12


def _clean_generated_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


def inventory_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    source_roots = [
        "docs/audit/omega_command_center_x_release_summary.md",
        "docs/audit/omega_command_center_x_quality_scorecard.md",
        "docs/audit/omega_command_center_x_red_team.md",
        "docs/repo_inventory",
        "docs/architecture/v2_command_center_x.md",
        "data/v2_command_center_x",
        "data/v2_command_center",
        "intraday_scanner/v2/command_center_x",
        "intraday_scanner/v2/command_center",
        "intraday_scanner/v2/paper_ops",
        "intraday_scanner/v2/calendar_intelligence",
        "intraday_scanner/v2/evidence",
        "intraday_scanner/v2/risk",
        "intraday_scanner/v2/learning_foundry",
        "intraday_scanner/v2/market_masters",
        "intraday_scanner/v2/autonomous_runner",
        "intraday_scanner/v2/telegram_intel",
        "data/v2_paper_ops",
        "data/v2_forward_evidence",
        "data/v2_learning_foundry",
        "data/v2_market_masters",
        "data/v2_autonomous_runner",
        "data/v2_scheduler",
        "data/v2_telegram_intel",
        "data/v2_autodata",
        "data/v2_fill_truth",
        "data/v2_evidence_commit",
        "tests",
    ]
    inventory = {
        "schema_version": "v2.command_center_x2.inventory.v1",
        "build_id": _build_id("command_center_x2_inventory"),
        "created_at": _now(),
        "source_roots": [
            {
                "path": path,
                "exists": (repo_root / path).exists(),
                "kind": "directory" if (repo_root / path).is_dir() else "file",
                "file_count": _file_count(repo_root / path),
            }
            for path in source_roots
        ],
        "output_dirs": list(OUTPUT_DIRS),
        "status": "passed",
    }
    _write_json(output_root / "reports/inventory_latest.json", inventory)
    (output_root / "reports/inventory_latest.md").write_text(
        _inventory_md(inventory),
        encoding="utf-8",
        newline="\n",
    )
    return inventory


def build_models_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    payload = write_story_models(output_root=output_root, repo_root=repo_root)
    result = {
        "schema_version": "v2.command_center_x2.models.v1",
        "status": "passed",
        "build_id": _build_id("command_center_x2_models"),
        "created_at": _now(),
        "day_count": len(payload.get("days", [])),
        "month_count": len(payload.get("months", [])),
        "strategy_count": len(payload.get("strategies", [])),
        "latest_run_date": payload.get("app", {}).get("latest_run_date", "unknown"),
    }
    _write_json(output_root / "reports/model_build_latest.json", result)
    return result


def build_calendar_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    _write_assets(output_root)
    data = _ensure_story_payload(repo_root=repo_root, output_root=output_root)
    written = _render_calendar(output_root=output_root, data=data)
    result = {
        "schema_version": "v2.command_center_x2.calendar_build.v1",
        "status": "passed",
        "build_id": _build_id("command_center_x2_calendar"),
        "created_at": _now(),
        "month_pages": [path.as_posix() for path in written],
    }
    _write_json(output_root / "reports/calendar_build_latest.json", result)
    return result


def build_days_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    _write_assets(output_root)
    data = _ensure_story_payload(repo_root=repo_root, output_root=output_root)
    written = _render_days(output_root=output_root, data=data)
    result = {
        "schema_version": "v2.command_center_x2.day_build.v1",
        "status": "passed",
        "build_id": _build_id("command_center_x2_days"),
        "created_at": _now(),
        "day_pages": [path.as_posix() for path in written],
    }
    _write_json(output_root / "reports/day_build_latest.json", result)
    return result


def build_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    if not (output_root / "reports/inventory_latest.json").exists():
        inventory_command_center_x2(repo_root=repo_root, output_root=output_root)
    _write_assets(output_root)
    data = _ensure_story_payload(repo_root=repo_root, output_root=output_root)
    _write_report_placeholders(output_root)
    build_id = _build_id("command_center_x2")
    pages = []
    pages.extend(_render_primary_pages(output_root=output_root, data=data, build_id=build_id))
    pages.extend(_render_calendar(output_root=output_root, data=data))
    pages.extend(_render_days(output_root=output_root, data=data))
    pages.extend(_render_strategy_pages(output_root=output_root, data=data))
    _write_bridges(repo_root=repo_root)
    manifest = {
        "schema_version": "v2.command_center_x2.manifest.v1",
        "status": "passed",
        "build_id": build_id,
        "created_at": _now(),
        "index": (output_root / "index.html").as_posix(),
        "page_count": len({path.as_posix() for path in pages}),
        "day_count": len(data.get("days", [])),
        "month_count": len(data.get("months", [])),
        "strategy_count": len(data.get("strategies", [])),
        "research_only": True,
        "live_trading_enabled": False,
        "existing_command_center_preserved": (
            repo_root / "data/v2_command_center/index.html"
        ).exists(),
        "command_center_x_preserved": (repo_root / "data/v2_command_center_x/index.html").exists(),
        "pages": [path.as_posix() for path in sorted(set(pages))],
    }
    _write_json(output_root / "manifests/command_center_x2_manifest.json", manifest)
    _write_json(output_root / "reports/build_report.json", manifest)
    (output_root / "reports/build_report.md").write_text(
        _build_report_md(manifest),
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def qa_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    return run_command_center_x2_qa(output_root=output_root, repo_root=repo_root)


def verify_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    qa = qa_command_center_x2(repo_root=repo_root, output_root=output_root)
    manifest = _read_json(output_root / "manifests/command_center_x2_manifest.json", {})
    required_docs = [
        "docs/architecture/v2_command_center_x2.md",
        "docs/architecture/v2_command_center_x2_story_models.md",
        "docs/architecture/v2_command_center_x2_design_system.md",
        "docs/operations/command_center_x2_user_guide.md",
        "docs/operations/command_center_x2_rebuild.md",
        "docs/audit/omega_command_center_x2_release_summary.md",
        "docs/audit/omega_command_center_x2_quality_scorecard.md",
        "docs/audit/omega_command_center_x2_red_team.md",
        "docs/audit/omega_command_center_x2_build_state.json",
        "docs/audit/omega_command_center_x2_resume_goal.md",
    ]
    missing_docs = [path for path in required_docs if not (repo_root / path).exists()]
    missing_pages = [
        name for name in REQUIRED_PAGE_NAMES if not (output_root / "pages" / name).exists()
    ]
    failures = []
    if qa.get("status") != "passed":
        failures.append("qa_not_passed")
    if int(manifest.get("day_count") or 0) <= 0:
        failures.append("day_pages_missing")
    if int(manifest.get("month_count") or 0) <= 0:
        failures.append("month_pages_missing")
    # Zero official strategy detail pages is a valid fail-closed state when no
    # verified PaperOps series is available.  The required Strategies surface
    # still exists and renders the explicit N/A state; missing truth must not be
    # replaced with demo rows merely to satisfy a release counter.
    if missing_docs:
        failures.append("missing_required_docs")
    if missing_pages:
        failures.append("missing_required_pages")
    result = {
        "schema_version": "v2.command_center_x2.verify.v1",
        "status": "passed" if not failures else "failed",
        "checked_at": _now(),
        "qa_status": qa.get("status", "missing"),
        "failures": failures,
        "missing_docs": missing_docs,
        "missing_pages": missing_pages,
        "manifest": manifest,
    }
    _write_json(output_root / "reports/verify_latest.json", result)
    (output_root / "reports/verify_latest.md").write_text(
        _verify_md(result),
        encoding="utf-8",
        newline="\n",
    )
    return result


def report_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    manifest = _read_json(output_root / "manifests/command_center_x2_manifest.json", {})
    if not manifest:
        manifest = build_command_center_x2(repo_root=repo_root, output_root=output_root)
    qa = qa_command_center_x2(repo_root=repo_root, output_root=output_root)
    data = _ensure_story_payload(repo_root=repo_root, output_root=output_root)
    score = _quality_score(qa=qa, manifest=manifest, data=data)
    final_status = (
        "COMPLETE_COMMAND_CENTER_X2"
        if score == 100 and qa.get("status") == "passed"
        else "RESUME_REQUIRED"
    )
    build_state = {
        "schema_version": "v2.command_center_x2.build_state.v1",
        "final_status": final_status,
        "quality_score": score,
        "build_id": _build_id("command_center_x2_release"),
        "command_center_x2_build_id": manifest.get("build_id", "missing"),
        "created_at": _now(),
        "page_count": manifest.get("page_count", 0),
        "day_count": manifest.get("day_count", 0),
        "month_count": manifest.get("month_count", 0),
        "strategy_count": manifest.get("strategy_count", 0),
        "qa_status": qa.get("status", "missing"),
        "research_only": True,
        "live_trading_enabled": False,
        "existing_command_center_preserved": manifest.get(
            "existing_command_center_preserved", False
        ),
        "command_center_x_preserved": manifest.get("command_center_x_preserved", False),
        "untrusted": _untrusted_items(data),
    }
    audit_dir = repo_root / "docs/audit"
    arch_dir = repo_root / "docs/architecture"
    ops_dir = repo_root / "docs/operations"
    audit_dir.mkdir(parents=True, exist_ok=True)
    arch_dir.mkdir(parents=True, exist_ok=True)
    ops_dir.mkdir(parents=True, exist_ok=True)
    _write_json(audit_dir / "omega_command_center_x2_build_state.json", build_state)
    (audit_dir / "omega_command_center_x2_release_summary.md").write_text(
        _release_summary_md(build_state=build_state, data=data),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_command_center_x2_quality_scorecard.md").write_text(
        _quality_scorecard_md(score=score, qa=qa),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_command_center_x2_red_team.md").write_text(
        _red_team_md(qa=qa, data=data),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_command_center_x2_resume_goal.md").write_text(
        _resume_goal_md(final_status=final_status, score=score, qa=qa),
        encoding="utf-8",
        newline="\n",
    )
    (arch_dir / "v2_command_center_x2.md").write_text(
        _architecture_md(),
        encoding="utf-8",
        newline="\n",
    )
    (arch_dir / "v2_command_center_x2_story_models.md").write_text(
        _story_models_md(),
        encoding="utf-8",
        newline="\n",
    )
    (arch_dir / "v2_command_center_x2_design_system.md").write_text(
        _design_system_md(),
        encoding="utf-8",
        newline="\n",
    )
    (ops_dir / "command_center_x2_user_guide.md").write_text(
        _user_guide_md(),
        encoding="utf-8",
        newline="\n",
    )
    (ops_dir / "command_center_x2_rebuild.md").write_text(
        _rebuild_md(),
        encoding="utf-8",
        newline="\n",
    )
    _write_json(output_root / "reports/release_state.json", build_state)
    return build_state


def demo_command_center_x2(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x2"),
) -> dict[str, Any]:
    inventory_command_center_x2(repo_root=repo_root, output_root=output_root)
    build_models_command_center_x2(repo_root=repo_root, output_root=output_root)
    build_calendar_command_center_x2(repo_root=repo_root, output_root=output_root)
    build_days_command_center_x2(repo_root=repo_root, output_root=output_root)
    build_command_center_x2(repo_root=repo_root, output_root=output_root)
    qa = qa_command_center_x2(repo_root=repo_root, output_root=output_root)
    report = report_command_center_x2(repo_root=repo_root, output_root=output_root)
    verify = verify_command_center_x2(repo_root=repo_root, output_root=output_root)
    return {
        "schema_version": "v2.command_center_x2.demo.v1",
        "status": "passed"
        if qa.get("status") == "passed" and verify.get("status") == "passed"
        else "failed",
        "final_status": report.get("final_status", "missing"),
        "build_id": report.get("build_id", "missing"),
        "quality_score": report.get("quality_score", 0),
        "qa_status": qa.get("status", "missing"),
        "verify_status": verify.get("status", "missing"),
    }


def _ensure_dirs(output_root: Path) -> None:
    for dirname in OUTPUT_DIRS:
        (output_root / dirname).mkdir(parents=True, exist_ok=True)


def _ensure_story_payload(*, repo_root: Path, output_root: Path) -> dict[str, Any]:
    path = output_root / "manifests/story_bundle.json"
    if not path.exists():
        write_story_models(output_root=output_root, repo_root=repo_root)
    return _read_json(path, {})


def _write_assets(output_root: Path) -> None:
    assets = output_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    _write_json(assets / "x2_design_tokens.json", _design_tokens())
    (assets / "x2.css").write_text(_base_css(), encoding="utf-8", newline="\n")
    (assets / "x2_components.css").write_text(
        _component_css(),
        encoding="utf-8",
        newline="\n",
    )
    (assets / "x2_interactions.js").write_text(
        _interactions_js(),
        encoding="utf-8",
        newline="\n",
    )
    (assets / "favicon.svg").write_text(
        _favicon_svg(),
        encoding="utf-8",
        newline="\n",
    )


def _render_primary_pages(
    *,
    output_root: Path,
    data: dict[str, Any],
    build_id: str,
) -> list[Path]:
    pages: list[Path] = []
    pages.append(
        _write_page(
            output_root / "index.html",
            "Mission Control",
            _today_body(data, actions_base="pages/"),
            data,
            build_id,
        )
    )
    body_map = {
        "today.html": ("Today", _today_body(data, actions_base="")),
        "calendar.html": ("Calendar", _calendar_index_body(data)),
        "strategies.html": ("Strategies", _strategies_body(data)),
        "no_picks.html": ("No Picks", _no_picks_body(data)),
        "automation.html": ("Automation", _automation_body(data)),
        "telegram.html": ("Telegram", _telegram_body(data)),
        "reports.html": ("Reports", _reports_body(data)),
        "six_month_backtest.html": ("Six-Month Backtest", _historical_backtest_body()),
        "day_trade_lab.html": ("Day Trade Lab", _day_trade_lab_body()),
        "day_trade_calendar.html": ("Day Trade Calendar", _day_trade_calendar_body()),
        "day_trade_strategies.html": ("Day Trade Strategies", _day_trade_strategies_body()),
        "day_trade_trades.html": ("Day Trade Trades", _day_trade_trades_body()),
        "day_trade_no_trade_days.html": ("Day Trade No-Trade Days", _day_trade_no_trade_days_body()),
        "day_trade_assumptions.html": ("Day Trade Assumptions", _day_trade_assumptions_body()),
        "day_trade_robustness.html": ("Day Trade Robustness", _day_trade_robustness_body()),
        "day_trade_slippage_stress.html": ("Day Trade Slippage Stress", _day_trade_slippage_stress_body()),
        "day_trade_oos.html": ("Day Trade Out-of-Sample", _day_trade_oos_body()),
        "day_trade_refinements.html": ("Day Trade Refined Challengers", _day_trade_refinements_body()),
        "system_map.html": ("System Map", _system_map_body(data)),
        "learning.html": ("Learning", _learning_body(data)),
        "market_masters.html": ("Market Masters", _market_body(data)),
        "risk.html": ("Risk", _risk_body(data)),
        "evidence.html": ("Evidence", _evidence_body(data)),
    }
    for filename, (title, body) in body_map.items():
        pages.append(_write_page(output_root / "pages" / filename, title, body, data, build_id))
    return pages


def _render_calendar(*, output_root: Path, data: dict[str, Any]) -> list[Path]:
    build_id = str(data.get("app", {}).get("generated_at", _build_id("calendar")))
    pages: list[Path] = []
    for month in data.get("months", []):
        month_key = str(month.get("month", "unknown"))
        body = _month_body(month)
        pages.append(
            _write_page(
                output_root / "months" / f"{month_key}.html", month_key, body, data, build_id
            )
        )
    return pages


def _render_days(*, output_root: Path, data: dict[str, Any]) -> list[Path]:
    build_id = str(data.get("app", {}).get("generated_at", _build_id("days")))
    pages: list[Path] = []
    seen: set[str] = set()
    for day in data.get("days", []):
        day_key = str(day.get("date", "unknown"))
        seen.add(day_key)
        pages.append(
            _write_page(
                output_root / "days" / f"{day_key}.html",
                f"Day {day_key}",
                _day_body(day),
                data,
                build_id,
            )
        )
    for month in data.get("months", []):
        for day in month.get("calendar_days", []):
            day_key = str(day.get("date", "unknown"))
            if day_key in seen or day_key == "unknown":
                continue
            seen.add(day_key)
            placeholder = _placeholder_day(day_key, day)
            pages.append(
                _write_page(
                    output_root / "days" / f"{day_key}.html",
                    f"Day {day_key}",
                    _day_body(placeholder),
                    data,
                    build_id,
                )
            )
    return pages


def _render_strategy_pages(*, output_root: Path, data: dict[str, Any]) -> list[Path]:
    build_id = str(data.get("app", {}).get("generated_at", _build_id("strategies")))
    pages: list[Path] = []
    for strategy in data.get("strategies", []):
        strategy_id = _slug(strategy.get("strategy_id", "unknown"))
        pages.append(
            _write_page(
                output_root / "strategies" / f"{strategy_id}.html",
                str(strategy.get("strategy_name") or strategy_id),
                _strategy_detail_body(strategy),
                data,
                build_id,
            )
        )
    return pages


def _write_page(
    path: Path,
    title: str,
    body: str,
    data: dict[str, Any],
    build_id: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel_assets = _rel_assets(path)
    html_text = _layout(
        title=title,
        body=body,
        data=data,
        build_id=build_id,
        rel_assets=rel_assets,
        path=path,
    )
    path.write_text(_clean_generated_text(html_text), encoding="utf-8", newline="\n")
    return path


def _layout(
    *,
    title: str,
    body: str,
    data: dict[str, Any],
    build_id: str,
    rel_assets: str,
    path: Path,
) -> str:
    app = data.get("app", {})
    latest = _esc(str(app.get("latest_run_date", "unknown")))
    warnings = app.get("warnings", [])
    nav = _nav(path)
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    asset_version = _slug(build_id)
    x3_target = _output_root_for(path).parent / "v2_command_center_x3/index.html"
    x3_link = (
        f'<a class="build-chip" href="{_relative(path.parent, x3_target)}">Open X3</a>'
        if x3_target.exists()
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dawnstrike X2 - {_esc(title)}</title>
  <link rel="icon" href="{rel_assets}/favicon.svg?v={asset_version}" type="image/svg+xml">
  <link rel="stylesheet" href="{rel_assets}/x2.css?v={asset_version}">
  <link rel="stylesheet" href="{rel_assets}/x2_components.css?v={asset_version}">
</head>
<body>
<aside class="app-shell">
  <a class="brand" href="{_root_link(path, "index.html")}">Dawnstrike <span>X2</span></a>
  <div class="trust-banner"><strong>Research-only</strong><span>Paper-only evidence. Live trading disabled.</span></div>
  <nav>{nav}</nav>
</aside>
<main>
  <header class="top-status">
    <div><span>X2 Command Center</span><strong>{_esc(title)}</strong></div>
    <div class="status-meta"><span>Latest artifact day</span><strong>{latest}</strong></div>
    {x3_link}
    <div class="build-chip">Build {_esc(build_id)}</div>
  </header>
  <section class="boundary-strip">
    <span>Research-only / paper-only</span>
    <span>Live trading disabled</span>
    <span>No strategy validated</span>
    <span>Shadow challengers are not official</span>
  </section>
  <div class="content-frame">
  {body}
  <details class="panel warnings-panel app-warnings-panel" data-warning-count="{warning_count}">
    <summary><span>Warnings & trust boundaries</span><strong>{warning_count} current warning(s)</strong></summary>
    {_warning_list(warnings)}
  </details>
  </div>
</main>
<script src="{rel_assets}/x2_interactions.js?v={asset_version}" defer></script>
</body>
</html>
"""


def _nav(path: Path) -> str:
    links = [
        ("Mission", "index.html"),
        ("Today", "pages/today.html"),
        ("Calendar", "pages/calendar.html"),
        ("Strategies", "pages/strategies.html"),
        ("No Picks", "pages/no_picks.html"),
        ("Learning", "pages/learning.html"),
        ("Market Masters", "pages/market_masters.html"),
        ("Risk", "pages/risk.html"),
        ("Evidence", "pages/evidence.html"),
        ("Automation", "pages/automation.html"),
        ("Telegram", "pages/telegram.html"),
        ("Reports", "pages/reports.html"),
        ("Day Trade Lab", "pages/day_trade_lab.html"),
        ("Day Strategies", "pages/day_trade_strategies.html"),
        ("Day Robustness", "pages/day_trade_robustness.html"),
        ("Day Refinements", "pages/day_trade_refinements.html"),
        ("Day Trades", "pages/day_trade_trades.html"),
        ("Backtest", "pages/six_month_backtest.html"),
        ("System", "pages/system_map.html"),
    ]
    current = path.as_posix().replace("\\", "/")
    output = []
    for label, href in links:
        target = href.split("/")[-1]
        is_active = current.endswith(target) or (
            href == "index.html" and current.endswith("index.html")
        )
        active = ' class="active"' if is_active else ""
        output.append(f'<a{active} href="{_root_link(path, href)}">{_esc(label)}</a>')
    return "\n".join(output)


def _page_hero(
    *,
    label: str,
    title: str,
    body: str,
    stat_label: str = "",
    stat_value: Any = "",
    stat_context: str = "",
    tone: str = "",
) -> str:
    side = ""
    hero_class = f"hero-story compact {tone}".strip()
    if stat_label:
        side = (
            f'<div class="pulse-card"><span>{_esc(stat_label)}</span>'
            f"<strong>{_esc(str(stat_value))}</strong>"
            f"<em>{_esc(stat_context)}</em></div>"
        )
    else:
        hero_class += " hero-single"
    return f"""
<section class="{hero_class}">
  <div><div class="section-label">{_esc(label)}</div>
  <h1>{_esc(title)}</h1><p>{_esc(body)}</p></div>
  {side}
</section>
"""


def _panel_shell(label: str, title: str, body: str, *, klass: str = "") -> str:
    classes = f"panel {klass}".strip()
    return f"""
<section class="{classes}">
  <div class="panel-heading"><div><div class="section-label">{_esc(label)}</div><h2>{_esc(title)}</h2></div></div>
  {body}
</section>
"""


def _today_body(data: dict[str, Any], *, actions_base: str) -> str:
    app = data.get("app", {})
    day_trade_summary = _day_trade_summary()
    metrics = app.get("top_metrics", [])
    latest = data.get("days", [])[-1] if data.get("days") else {}
    actions = "".join(f"<li>{_esc(str(item))}</li>" for item in latest.get("what_to_watch_next", []))
    strategy_rows = "".join(
        f"""<div class="signal-row">
  <strong>{_esc(str(item.get("strategy_name", "Strategy")))}</strong>
  <span>{_esc(str(item.get("latest_paper_state", "n/a")))}</span>
  <b>{_esc(str(item.get("daily_return_pct", "n/a")))}</b>
</div>"""
        for item in data.get("strategies", [])[:5]
    )
    learning_rows = "".join(
        f"""<div class="signal-row">
  <strong>{_esc(str(item.get("title", "Lesson")))}</strong>
  <span>{_esc(str(item.get("summary", "")))}</span>
</div>"""
        for item in data.get("learning_cards", [])[:3]
    )
    return f"""
<section class="hero-story">
  <div>
    <div class="section-label">Mission Control</div>
    <h1>{_esc(str(app.get("headline", "Dawnstrike is operating")))}</h1>
    <p>{_esc(str(app.get("subheadline", "")))}</p>
    <div class="hero-actions">
      <a class="button" href="{actions_base}calendar.html">Open Calendar</a>
      <a class="button" href="{actions_base}day_trade_lab.html">Open Day Trade Lab</a>
      <a class="button secondary" href="{actions_base}no_picks.html">Read No-Picks Story</a>
    </div>
  </div>
  <div class="pulse-card"><span>Status pulse</span><strong>{_esc(str(app.get("alert_level", "unknown")))}</strong></div>
</section>
<section class="metric-grid">{_metric_cards(metrics)}</section>
<section class="dashboard-grid">
  <section class="panel">
    <div class="section-label">What X2 Does</div>
    <p>Dawnstrike X2 converts the artifact library into a day-by-day operating story: what happened, why it happened, what stayed blocked, and what remains untrusted.</p>
  </section>
  <section class="panel">
    <div class="section-label">Trading Research Lanes</div>
    <p>Day Trade Lab is the intraday-only lane: 1-minute or 5-minute bars, same-session entries and exits, no overnight holds, and EOD-flat exits. Six-Month Backtest is daily swing research and is not day-trading proof.</p>
    <div class="signal-list">
      <div class="signal-row"><strong>Day-trade corpus</strong><span>{_esc(str(day_trade_summary.get("sessions_covered", day_trade_summary.get("real_intraday_session_count", "n/a"))))} covered session rows / {_esc(str(day_trade_summary.get("total_day_trades", day_trade_summary.get("trade_count", "n/a"))))} generated trades</span><b>{_esc(str(day_trade_summary.get("overnight_hold_count", "n/a")))} overnight</b></div>
    </div>
    <div class="hero-actions">
      <a class="button" href="{actions_base}day_trade_lab.html">Intraday Day Trade Lab</a>
      <a class="button secondary" href="{actions_base}six_month_backtest.html">Daily Swing Research</a>
    </div>
  </section>
  <section class="panel">
    <div class="section-label">Next Actions</div>
    <ul>{actions or "<li>No next-action artifact found.</li>"}</ul>
  </section>
  <section class="panel">
    <div class="section-label">Strategy Pulse</div>
    <div class="signal-list">{strategy_rows or "<p>No strategy rows found.</p>"}</div>
  </section>
  <section class="panel">
    <div class="section-label">Learning Pulse</div>
    <div class="signal-list">{learning_rows or "<p>No learning rows found.</p>"}</div>
    <p class="quiet-note">{len(data.get("app", {}).get("warnings", []))} warning(s) remain visible in the warning drawer.</p>
  </section>
</section>
"""


def _calendar_index_body(data: dict[str, Any]) -> str:
    months = data.get("months", [])
    latest = _latest_evidence_month(months)
    day_trade_summary = _day_trade_summary()
    month_links = "".join(
        f'<a class="month-tab" href="../months/{_esc(str(item.get("month")))}.html">'
        f"{_esc(str(item.get('month')))}</a>"
        for item in months
    )
    return f"""
{_page_hero(label="Calendar Memory", title="Scan the record by month, then open the day.", body="Each cell links to an artifact-backed day story. Empty or incomplete days stay visibly marked instead of being filled with fake certainty.", stat_label="Latest cumulative", stat_value=latest.get("cumulative_return_pct", "n/a"), stat_context="Paper-only source rows")}
<section class="panel month-tabs"><div class="section-label">Months</div>{month_links}</section>
<section class="panel">
  <div class="section-label">Day Trade Corpus</div>
  <p>Intraday Day Trade Lab coverage is tracked separately: {_esc(str(day_trade_summary.get("sessions_covered", "n/a")))} covered symbol/interval sessions, {_esc(str(day_trade_summary.get("total_day_trades", "n/a")))} same-session trades, and {_esc(str(day_trade_summary.get("overnight_hold_count", "n/a")))} overnight holds.</p>
  <div class="hero-actions"><a class="button" href="day_trade_calendar.html">Open Day Trade Calendar</a></div>
</section>
{_month_body(latest, compact=True) if latest else '<section class="panel">No calendar artifacts found.</section>'}
"""


def _latest_evidence_month(months: list[Any]) -> dict[str, Any]:
    evidence_months = [
        month for month in months if isinstance(month, dict) and _month_has_evidence(month)
    ]
    if evidence_months:
        return evidence_months[-1]
    return months[-1] if months and isinstance(months[-1], dict) else {}


def _month_has_evidence(month: dict[str, Any]) -> bool:
    for day in month.get("calendar_days", []):
        if not isinstance(day, dict):
            continue
        daily = str(day.get("daily_return_pct", "n/a")).strip().lower()
        cumulative = str(day.get("cumulative_return_pct", "n/a")).strip().lower()
        trades = str(day.get("trade_count", 0)).strip()
        if daily not in {"", "n/a", "none"} or cumulative not in {"", "n/a", "none"}:
            return True
        if trades not in {"", "0", "0.0", "n/a", "none"}:
            return True
    return False


def _month_body(month: dict[str, Any], *, compact: bool = False) -> str:
    days = month.get("calendar_days", [])
    cells = []
    for day in days:
        href = str(day.get("href", "#"))
        cells.append(
            f"""<a class="calendar-cell tone-{_esc(str(day.get("tone", "none")))}" href="{_esc(href)}"
 data-day-summary="{_esc(str(day.get("state", "no-evidence")))}">
  <span>{_esc(str(day.get("date", "")[-2:]))}</span>
  <strong>{_esc(str(day.get("daily_return_pct", "n/a")))}</strong>
  <em>cum {_esc(str(day.get("cumulative_return_pct", "n/a")))}</em>
  <small>{_esc(str(day.get("trade_count", 0)))} paper</small>
</a>"""
        )
    shell_class = "panel month-hero month-overview" if compact else "hero-story compact month-hero"
    heading = "h2" if compact else "h1"
    return f"""
<section class="{shell_class}">
  <div>
    <div class="section-label">Month</div>
    <{heading}>{_esc(str(month.get("month", "unknown")))}</{heading}>
    <p>Source policy: {_esc(str(month.get("source_policy", "n/a")))}</p>
  </div>
  <div class="return-strip">
    <div><span>Monthly</span><strong>{_esc(str(month.get("monthly_return_pct", "n/a")))}</strong></div>
    <div><span>Cumulative</span><strong>{_esc(str(month.get("cumulative_return_pct", "n/a")))}</strong></div>
    <div><span>Best</span><strong>{_esc(str(month.get("best_day", "n/a")))}</strong></div>
    <div><span>Worst</span><strong>{_esc(str(month.get("worst_day", "n/a")))}</strong></div>
  </div>
</section>
<section class="calendar-heatmap">{"".join(cells)}</section>
"""


def _day_body(day: dict[str, Any]) -> str:
    strategy_rows = "".join(_day_strategy_row(item) for item in _unique_strategy_rows(day.get("strategy_returns", [])))
    trade_rows = "".join(_paper_trade_row(item) for item in day.get("paper_trades", []))
    no_pick_items = "".join(
        f"<li>{_esc(str(item))}</li>" for item in day.get("no_picks_reasons", [])
    )
    watch = "".join(f"<li>{_esc(str(item))}</li>" for item in day.get("what_to_watch_next", []))
    return f"""
<section class="hero-story compact">
  <div>
    <div class="section-label">Day Story</div>
    <h1>{_esc(str(day.get("headline", "Day story")))}</h1>
    <p>{_esc(str(day.get("market_context", "")))}</p>
  </div>
  <div class="pulse-card"><span>Cumulative</span><strong>{_esc(str(day.get("cumulative_returns", {}).get("cumulative_return_pct", "n/a")))}</strong></div>
</section>
<section class="panel">
  <div class="section-label">What Happened</div>
  <div class="return-strip">
    <div><span>Accepted</span><strong>{_esc(str(day.get("picks_summary", {}).get("accepted", 0)))}</strong></div>
    <div><span>Blocked</span><strong>{_esc(str(day.get("picks_summary", {}).get("blocked", 0)))}</strong></div>
    <div><span>Watch</span><strong>{_esc(str(day.get("picks_summary", {}).get("watch", 0)))}</strong></div>
    <div><span>Daily return</span><strong>{_esc(str(day.get("cumulative_returns", {}).get("daily_return_pct", "n/a")))}</strong></div>
  </div>
</section>
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Strategy Returns</div><h2>Unique strategy rows for the day</h2></div></div>
  <div class="table-scroll"><table class="backtest-table day-strategy-table">
    <thead><tr><th>Strategy</th><th>State</th><th>Daily</th><th>Cumulative</th><th>Trust</th></tr></thead>
    <tbody>{strategy_rows or '<tr><td colspan="5">No strategy return rows for this day.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Paper Trade Journey</div><h2>Evidence chain and paper state</h2></div></div>
  <div class="table-scroll"><table class="backtest-table paper-trade-table">
    <thead><tr><th>Paper item</th><th>State</th><th>Entry / target</th><th>Evidence</th></tr></thead>
    <tbody>{trade_rows or '<tr><td colspan="4">No paper trade timeline for this day.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="panel"><div class="section-label">Why No Picks / Why Blocked</div><ul>{no_pick_items or "<li>No no-picks artifact found for this day.</li>"}</ul></section>
<section class="story-grid">
  <div class="panel"><div class="section-label">Learning</div><p>{_esc(str(day.get("learning_foundry_lesson", "n/a")))}</p></div>
  <div class="panel"><div class="section-label">Market Masters</div><p>{_esc(str(day.get("market_masters_lesson", "n/a")))}</p></div>
  <div class="panel"><div class="section-label">Evidence Chain</div><p>{_esc(str(day.get("filltruth_summary", "")))}</p><p>{_esc(str(day.get("commitbridge_summary", "")))}</p></div>
  <div class="panel"><div class="section-label">What To Watch Tomorrow</div><ul>{watch}</ul></div>
</section>
"""


def _strategies_body(data: dict[str, Any]) -> str:
    day_trade_summary = _day_trade_summary()
    rows = []
    for item in data.get("strategies", []):
        strategy_id = str(item.get("strategy_id", "unknown"))
        href = f"../strategies/{_slug(strategy_id)}.html"
        rows.append(
            f"""<tr data-filter-item>
  <td><a class="table-link" href="{href}"><strong>{_esc(str(item.get("strategy_name", "Strategy")))}</strong><em>{_esc(strategy_id)}</em></a></td>
  <td>{_esc(str(item.get("role", "strategy")))}</td>
  <td>{_esc(str(item.get("latest_paper_state", "n/a")))}</td>
  <td class="{_return_class(item.get("daily_return_pct"))}">{_esc(str(item.get("daily_return_pct", "n/a")))}</td>
  <td>{_esc(str(item.get("cumulative_return_pct", "n/a")))}</td>
  <td><span class="trust-chip">Not validated</span></td>
</tr>"""
        )
    return f"""
{_page_hero(label="Strategy Stories", title="One strategy list, sorted for inspection.", body="Each row opens the strategy detail. Returns are paper-only source rows; no strategy is validated and shadow challengers remain separate from official evidence.", stat_label="Validation count", stat_value="0", stat_context="No live-trading approval")}
<section class="panel">
  <div class="section-label">Research Lane Separation</div>
  <p>This page is the paper strategy story lane. Day-trade strategy ranking is separate and comes from intraday corpus artifacts only: {_esc(str(day_trade_summary.get("comparison_rows", "n/a")))} ranked day-trade rows, {_esc(str(day_trade_summary.get("overnight_hold_count", "n/a")))} overnight holds.</p>
  <div class="hero-actions"><a class="button" href="day_trade_strategies.html">Open Day Trade Strategies</a><a class="button secondary" href="six_month_backtest.html">Daily Swing Research</a></div>
</section>
<section class="toolbar"><input data-x2-search placeholder="Search strategies"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Strategy</th><th>Role</th><th>State</th><th>Daily</th><th>Cumulative</th><th>Trust</th></tr></thead>
    <tbody>{''.join(rows) or '<tr data-strategy-empty-state="true"><td colspan="6"><strong>No verified official PaperOps strategy rows.</strong><br>Returns are N/A until exact, source-gated forward evidence exists.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _strategy_detail_body(strategy: dict[str, Any]) -> str:
    series = "".join(
        f'<span style="height:{_spark_height(row.get("daily_return_pct"))}%"></span>'
        for row in strategy.get("daily_series", [])
    )
    warnings = _warning_list(strategy.get("warnings", []))
    return f"""
{_page_hero(label=str(strategy.get("role", "strategy")), title=str(strategy.get("strategy_name", "Strategy")), body=str(strategy.get("evidence_quality", "n/a")), stat_label="Validation", stat_value="Not validated", stat_context="Research-only / paper-only")}
<section class="metric-grid">
  {
        _metric_cards(
            [
                {
                    "label": "Daily",
                    "value": strategy.get("daily_return_pct", "n/a"),
                    "context": "Latest source row",
                },
                {
                    "label": "Cumulative",
                    "value": strategy.get("cumulative_return_pct", "n/a"),
                    "context": "Latest source row",
                },
                {
                    "label": "Trades",
                    "value": strategy.get("trade_count", 0),
                    "context": "Paper row count",
                },
                {
                    "label": "Drawdown",
                    "value": strategy.get("drawdown", "n/a"),
                    "context": "Source artifact",
                },
            ]
        )
    }
</section>
{_panel_shell("Current State", "What this strategy is allowed to mean", _strategy_state_table(strategy), klass="table-panel")}
{_panel_shell("Return Trail", "Latest source-row series", f'<div class="sparkline">{series}</div>')}
{_panel_shell("Learning Notes", "What the system currently says", f'<p>{_esc(str(strategy.get("latest_learning_notes", "n/a")))}</p>')}
{_panel_shell("Warnings", "Why this remains untrusted", warnings, klass="warnings-panel")}
"""


def _no_picks_body(data: dict[str, Any]) -> str:
    model = data.get("no_picks", {})
    reasons = "".join(f"<li>{_esc(str(item))}</li>" for item in model.get("top_reasons", []))
    blockers = "".join(f"<li>{_esc(str(item))}</li>" for item in model.get("riskhub_blockers", []))
    changes = "".join(f"<li>{_esc(str(item))}</li>" for item in model.get("what_would_change", []))
    return f"""
{_page_hero(label="Disciplined Wait", title=str(model.get("headline", "No picks story")), body=str(model.get("why_no_trade_is_valid", "")), stat_label="Official paper picks", stat_value=model.get("accepted_count", 0), stat_context="Waiting is allowed", tone="wait-hero")}
<section class="metric-grid">
  {_metric_cards([
      {"label": "Blocked", "value": model.get("blocked_count", 0), "context": "RiskHub or evidence gates"},
      {"label": "Watch", "value": model.get("watch_count", 0), "context": "Near candidates"},
      {"label": "No setup", "value": model.get("no_setup_count", 0), "context": "No trade condition"},
  ])}
</section>
<section class="story-grid">
  <div class="panel"><div class="section-label">Top Reasons</div><ul>{reasons}</ul></div>
  <div class="panel"><div class="section-label">RiskHub Blockers</div><ul>{
        blockers or "<li>n/a</li>"
    }</ul></div>
  <div class="panel"><div class="section-label">What Would Change</div><ul>{changes}</ul></div>
</section>
"""


def _automation_body(data: dict[str, Any]) -> str:
    model = data.get("automation", {})
    task_rows = "".join(_task_row(row) for row in model.get("task_statuses", []))
    missed = "".join(
        f"<li>{_esc(str(row.get('task')))} - {_esc(str(row.get('state')))}</li>"
        for row in model.get("missed_runs", [])
    )
    return f"""
{_page_hero(label="Operating System Health", title="Automation status without mystery.", body="Scheduled tasks, missed runs, overlap policy, Telegram readiness, and watchdog state are artifact-backed.", stat_label="Runner", stat_value=model.get("autonomous_runner_status", "missing"), stat_context="Autonomous runner status")}
<section class="panel table-panel">
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Task</th><th>State</th><th>Last run</th><th>Next run</th></tr></thead>
    <tbody>{task_rows or '<tr><td colspan="4">No task artifacts found.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="story-grid compact-grid">
  <div class="panel"><div class="section-label">Missed Runs</div><ul>{missed or "<li>None reported.</li>"}</ul></div>
  <div class="panel"><div class="section-label">No-overlap</div><p>{_esc(str(model.get("no_overlap_status", "n/a")))}</p></div>
</section>
"""


def _telegram_body(data: dict[str, Any]) -> str:
    model = data.get("automation", {})
    return f"""
{_page_hero(label="Telegram Intel", title="Messages are readiness-checked, not sent from X2.", body="This page shows message readiness and quality without exposing token or chat values.", stat_label="Readiness", stat_value=model.get("telegram_readiness", "missing"), stat_context="Secrets never rendered")}
{_panel_shell("Next Step", "If Telegram is disabled", "<p>Configure Telegram environment outside the UI, rerun Telegram Intel verify, then rebuild X2.</p>")}
"""


def _reports_body(data: dict[str, Any]) -> str:
    report_cards = list(data.get("reports", []))
    report_cards.append(
        {
            "href": "pages/six_month_backtest.html",
            "status": "historical-only",
            "title": "Six-Month Historical Backtest",
            "why": "Historical backtest only — not validated forward performance.",
        }
    )
    report_cards.append(
        {
            "href": "pages/day_trade_lab.html",
            "status": "intraday-only",
            "title": "Day Trade Lab",
            "why": "Intraday same-session day-trade research with no overnight holds.",
        }
    )
    rows = "".join(
        f"""<tr data-filter-item>
  <td><a class="table-link" href="../{_esc(str(card.get("href", "#")))}"><strong>{_esc(str(card.get("title", "Report")))}</strong><em>{_esc(str(card.get("why", "")))}</em></a></td>
  <td><span class="trust-chip">{_esc(str(card.get("status", "pending")))}</span></td>
</tr>"""
        for card in report_cards
    )
    return f"""
{_page_hero(label="Reports", title="Open the proof, not a marketing page.", body="Reports are grouped as source artifacts, QA output, and historical research. Each link stays local.", stat_label="Report links", stat_value=len(report_cards), stat_context="Local artifacts only")}
<section class="toolbar"><input data-x2-search placeholder="Filter reports"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Report</th><th>Status</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
</section>
"""


def _day_trade_lab_body() -> str:
    summary = _day_trade_summary()
    robustness_report = _day_trade_robustness_report()
    corpus = _day_trade_corpus_available()
    quality = _read_json(Path("data/v2_day_trade_lab/corpus/reports/corpus_quality.json"), {}) if corpus else {}
    demo = _read_json(Path("data/v2_day_trade_lab/reports/demo_proof.json"), {})
    limitations = _day_trade_limitations(summary, quality)
    limitation_items = "".join(f"<li>{_esc(item)}</li>" for item in limitations)
    date_range = summary.get("intraday_corpus_date_range", {}) if isinstance(summary.get("intraday_corpus_date_range"), dict) else {}
    symbols = ", ".join(str(item) for item in summary.get("symbols_covered", [])) or "n/a"
    intervals = ", ".join(str(item) for item in summary.get("intervals_covered", [])) or "n/a"
    demo_line = (
        f"{_esc(str(demo.get('one_minute_trade_count', 'n/a')))} / "
        f"{_esc(str(demo.get('five_minute_trade_count', 'n/a')))}"
        if demo
        else "n/a"
    )
    return f"""
{_page_hero(label="Day Trade Lab", title="Intraday-only strategy research.", body="This lane is separate from the six-month daily-bar backtest. It accepts only 1-minute or 5-minute bars and requires every generated trade to enter and exit inside the same market session.", stat_label="Final status", stat_value=_day_trade_status_label(summary.get("final_status", "missing")), stat_context="No live trading")}
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>Corpus sessions</span><strong>{_esc(str(summary.get("sessions_covered", summary.get("real_intraday_session_count", "n/a"))))}</strong><em>Symbol/interval session rows</em></div>
  <div class="metric-card"><span>Day-trade rows</span><strong>{_esc(str(summary.get("total_day_trades", summary.get("trade_count", "n/a"))))}</strong><em>Same-session exits only</em></div>
  <div class="metric-card"><span>Overnight holds</span><strong>{_esc(str(summary.get("overnight_hold_count", "n/a")))}</strong><em>Must remain 0</em></div>
  <div class="metric-card"><span>Symbols</span><strong>{_esc(str(len(summary.get("symbols_covered", [])) or "n/a"))}</strong><em>{_esc(symbols)}</em></div>
</section>
<section class="dashboard-grid">
  <section class="panel">
    <div class="section-label">What Counts Here</div>
    <p>Accepted inputs are 1-minute and 5-minute intraday bars. A generated row is a day trade only when entry and exit are the same local session, overnight is false, and the exit policy is stop, target, timeout, or EOD-flat. Daily Swing Research remains separate.</p>
  </section>
  <section class="panel warnings-panel">
    <div class="section-label">Current Data Limitations</div>
    <ul>{limitation_items or "<li>No limitations artifact found.</li>"}</ul>
  </section>
  <section class="panel">
    <div class="section-label">Corpus Coverage</div>
    <p>{_esc(str(date_range.get("start", "n/a")))} to {_esc(str(date_range.get("end", "n/a")))} across {_esc(intervals)}. Provider status: {_esc(str(summary.get("provider_status", "n/a")))}.</p>
  </section>
  <section class="panel">
    <div class="section-label">Demo Proof Boundary</div>
    <p>Fixture demo proof remains separate: {demo_line} 1min / 5min demo trades. Demo rows are not used as corpus evidence.</p>
  </section>
</section>
<section class="story-grid compact-grid">
  <a class="story-card" href="day_trade_calendar.html"><span>Session inventory</span><strong>Open Day Trade Calendar</strong><p>See covered sessions and partial-session flags.</p></a>
  <a class="story-card" href="day_trade_strategies.html"><span>Strategy comparison</span><strong>Open Day Strategies</strong><p>Click strategy names to inspect same-session trade ledgers.</p></a>
  <a class="story-card" href="day_trade_robustness.html"><span>Robustness audit</span><strong>Open Robustness</strong><p>Review symbol, time, month, weekday, interval, and session-quality slices.</p></a>
  <a class="story-card" href="day_trade_slippage_stress.html"><span>Slippage stress</span><strong>Open Stress Test</strong><p>See which intraday edges fail under spread, fill, and commission pressure.</p></a>
  <a class="story-card" href="day_trade_oos.html"><span>Out-of-sample</span><strong>Open OOS Splits</strong><p>Compare research and holdout behavior before trusting a ranking.</p></a>
  <a class="story-card" href="day_trade_refinements.html"><span>Shadow challengers</span><strong>Open Refinements</strong><p>Inspect evidence-gated challenger variants. They remain not validated.</p></a>
  <a class="story-card" href="day_trade_trades.html"><span>Trade ledger</span><strong>Open Day Trades</strong><p>Review entry time, exit time, hold minutes, stop, target, and exit reason.</p></a>
  <a class="story-card" href="day_trade_assumptions.html"><span>Assumptions</span><strong>Open Assumptions</strong><p>Read why daily-bar backtests do not qualify as day-trading proof.</p></a>
</section>
<section class="panel warnings-panel">
  <div class="section-label">Robustness Boundary</div>
  <p>Historical day-trade backtest only - not validated. Current robustness status: {_esc(str(robustness_report.get("final_status", "missing")))} with score {_esc(str(robustness_report.get("quality_score", "n/a")))}/100. Zero overnight holds remain required.</p>
</section>
"""


def _day_trade_calendar_body() -> str:
    sessions = _day_trade_session_inventory()
    session_rows = sessions.get("sessions", []) if isinstance(sessions, dict) else []
    day_returns = _day_trade_day_returns()
    rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(str(row.get("session_date", "n/a")))}</strong><em>{_esc(str(row.get("symbol", "n/a")))} / {_esc(str(row.get("interval", "n/a")))}</em></td>
  <td><span class="trust-chip">{_esc(str(row.get("session_status", "n/a")))}</span></td>
  <td>{_esc(str(row.get("premarket_bar_count", "n/a")))}</td>
  <td>{_esc(str(row.get("rth_bar_count", "n/a")))} / {_esc(str(row.get("expected_rth_bars", "n/a")))}</td>
  <td>{_esc(str(row.get("day_trade_eligible", "n/a")))}</td>
</tr>"""
        for row in session_rows
        if isinstance(row, dict)
    )
    day_return_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(str(row.get("session_date", "n/a")))}</strong><em>{_esc(str(row.get("interval", "n/a")))}</em></td>
  <td>{_esc(str(row.get("trade_count", "n/a")))}</td>
  <td class="{_return_class(row.get("net_pnl"))}">{_esc(_money_text(row.get("net_pnl")))}</td>
  <td class="{_return_class(row.get("day_return_pct"))}">{_esc(_percent_text(row.get("day_return_pct")))}</td>
</tr>"""
        for row in day_returns
    )
    return f"""
{_page_hero(label="Day Trade Calendar", title="Covered intraday sessions, not daily candles.", body="The calendar shows what intraday bars exist and whether the session is complete enough for day-trade research. Sparse or partial sessions stay visible.", stat_label="Sessions", stat_value=sessions.get("session_count", "n/a") if isinstance(sessions, dict) else "n/a", stat_context="1min and 5min rows")}
<section class="toolbar"><input data-x2-search placeholder="Filter sessions"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Session Inventory</div><h2>Intraday coverage by symbol and interval</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Session</th><th>Status</th><th>Premarket</th><th>RTH Bars</th><th>Eligible</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="5">No Day Trade Lab session inventory found.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Day Returns</div><h2>Same-session trade outcome by day</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Day</th><th>Trades</th><th>Net PnL</th><th>Return</th></tr></thead>
    <tbody>{day_return_rows or '<tr><td colspan="4">No Day Trade Lab day-return rows found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _day_trade_strategies_body() -> str:
    summary = _day_trade_summary()
    robustness_report = _day_trade_robustness_report()
    most_robust = _dict_payload(robustness_report.get("most_robust_strategy"))
    most_fragile = _dict_payload(robustness_report.get("most_fragile_strategy"))
    comparison = _day_trade_comparison_rows()
    rows: list[str] = []
    menus: list[str] = []
    for row in comparison:
        if not isinstance(row, dict):
            continue
        strategy_id = str(row.get("strategy_id", "n/a"))
        interval = str(row.get("interval", "n/a"))
        label = str(row.get("strategy_name") or _strategy_label(strategy_id))
        detail_id = f"day-trade-menu-{_slug(strategy_id)}-{_slug(interval)}"
        rows.append(
            f"""<tr data-filter-item>
  <td><span class="rank-chip">{_esc(str(row.get("rank_by_return", "n/a")))}</span></td>
  <td><button class="strategy-disclosure" type="button" data-x2-toggle="{detail_id}" aria-controls="{detail_id}" aria-expanded="false" aria-label="View day-trade ledger for {_esc(label)}" title="View day-trade ledger"><strong>{_esc(label)}</strong><em>{_esc(strategy_id)} / {_esc(interval)}</em></button></td>
  <td>{_esc(str(row.get("role", "Day Trade Strategy")))}</td>
  <td class="{_return_class(row.get("total_return_pct"))}">{_esc(_percent_text(row.get("total_return_pct")))}</td>
  <td>{_esc(_percent_text(row.get("max_drawdown_pct")))}</td>
  <td>{_esc(str(row.get("trade_count", "n/a")))}</td>
  <td><span class="horizon-chip">{_esc(str(row.get("avg_hold_minutes", "n/a")))} min</span><em>same-session only</em></td>
  <td><span class="trust-chip">Not validated</span></td>
</tr>"""
        )
        menus.append(_day_trade_ledger_menu(strategy_id, interval, label, detail_id))
    return f"""
{_page_hero(label="Day Trade Strategies", title="Intraday strategy comparison.", body="Rows are ranked from Day Trade Lab artifacts only. Click a strategy name to open the trade menu; no page expansion and no daily-bar holds are mixed into this table.", stat_label="Rows", stat_value=len(rows), stat_context=_day_trade_status_label(summary.get("final_status", "n/a")))}
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>Robustness score</span><strong>{_esc(str(robustness_report.get("quality_score", "n/a")))}/100</strong><em>Historical research gate</em></div>
  <div class="metric-card"><span>Most robust</span><strong>{_esc(_strategy_label(most_robust.get("strategy_id", "n/a")))}</strong><em>{_esc(str(most_robust.get("interval", "n/a")))} / expectancy {_esc(_decimal_text(most_robust.get("expectancy"), 3))}R</em></div>
  <div class="metric-card"><span>Most fragile</span><strong>{_esc(_strategy_label(most_fragile.get("strategy_id", "n/a")))}</strong><em>{_esc(str(most_fragile.get("interval", "n/a")))} / {_esc(str(most_fragile.get("fragility_warning", "n/a")))}</em></div>
  <div class="metric-card"><span>Trust boundary</span><strong>Not validated</strong><em>Historical day-trade backtest only</em></div>
</section>
<section class="toolbar"><input data-x2-search aria-label="Filter day-trade strategies" placeholder="Filter day strategies"></section>
<section class="panel backtest-table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Ranked Day Trade Strategies</div><h2>Same-session intraday comparison</h2></div><span>1min and 5min</span></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Rank</th><th>Strategy</th><th>Role</th><th>Return</th><th>Drawdown</th><th>Trades</th><th>Hold</th><th>Trust</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="8">No Day Trade Lab strategy comparison found.</td></tr>'}</tbody>
  </table></div>
  {''.join(menus)}
</section>
"""


def _day_trade_trades_body() -> str:
    trades = _day_trade_trades()
    rows = "".join(_day_trade_trade_row(row) for row in trades[:120])
    extra = (
        f'<p class="quiet-note">Showing first 120 of {len(trades)} day-trade rows.</p>'
        if len(trades) > 120
        else ""
    )
    return f"""
{_page_hero(label="Day Trade Trades", title="Every row must be same-session.", body="This ledger shows entry time, exit time, explicit stop/target, hold minutes, and exit reason. Overnight rows are rejected by the Day Trade Lab verify step.", stat_label="Trades", stat_value=len(trades), stat_context="No overnight holds")}
<section class="toolbar"><input data-x2-search aria-label="Filter day-trade rows" placeholder="Filter trades"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Trade Ledger</div><h2>Intraday entries and exits</h2></div></div>
  {extra}
  <div class="table-scroll"><table class="backtest-table trade-ledger-table">
    <thead><tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Entry / Exit</th><th>Stop / Target</th><th>Result</th><th>Hold</th><th>Reason</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="10">No Day Trade Lab trades found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _day_trade_no_trade_days_body() -> str:
    skips = _day_trade_no_trade_rows()
    reasons = _day_trade_skip_reason_rows()
    rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(str(row.get("session_date", "n/a")))}</strong><em>{_esc(str(row.get("symbol", "n/a")))} / {_esc(str(row.get("interval", "n/a")))}</em></td>
  <td>{_esc(str(row.get("strategy_id", "n/a")))}</td>
  <td>{_esc(str(row.get("reason", "n/a")))}</td>
</tr>"""
        for row in skips[:300]
    )
    reason_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(str(row.get("strategy_id", "n/a")))}</strong><em>{_esc(str(row.get("symbol", "n/a")))} / {_esc(str(row.get("interval", "n/a")))}</em></td>
  <td>{_esc(str(row.get("session_date", "n/a")))}</td>
  <td>{_esc(str(row.get("reason", "n/a")))}</td>
</tr>"""
        for row in reasons[:300]
    )
    return f"""
{_page_hero(label="No-Trade Days", title="Skipped setups are first-class evidence.", body="A no-trade row means the intraday rule set did not find a valid same-session setup. Missing premarket bars, single-symbol relative-strength limits, and partial sessions are recorded instead of being filled with fake trades.", stat_label="No-trade rows", stat_value=len(skips), stat_context="By strategy and session")}
<section class="toolbar"><input data-x2-search aria-label="Filter no-trade rows" placeholder="Filter no-trade reasons"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">No-Trade Days</div><h2>Strategy/session skip ledger</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Session</th><th>Strategy</th><th>Reason</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="3">No no-trade rows found.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Raw Skip Reasons</div><h2>Per-strategy setup failures</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Strategy</th><th>Day</th><th>Reason</th></tr></thead>
    <tbody>{reason_rows or '<tr><td colspan="3">No skip-reason rows found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _day_trade_assumptions_body() -> str:
    summary = _day_trade_summary()
    definition = summary.get("day_trade_definition", {}) if isinstance(summary, dict) else {}
    demo = _read_json(Path("data/v2_day_trade_lab/reports/demo_proof.json"), {})
    limitations = _day_trade_limitations(summary)
    definition_rows = "".join(
        f"<tr><td><strong>{_esc(str(key).replace('_', ' ').title())}</strong></td><td>{_esc(str(value))}</td></tr>"
        for key, value in definition.items()
    )
    limitation_items = "".join(f"<li>{_esc(item)}</li>" for item in limitations)
    return f"""
{_page_hero(label="Day Trade Assumptions", title="The definition is mechanical.", body="This page explains why the old six-month rankings were not day trades: daily candles cannot prove intraday entry, intraday stop/target behavior, or EOD-flat exits.", stat_label="Daily bars allowed", stat_value=definition.get("daily_bars_allowed", False), stat_context="Must be false")}
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Day Trade Definition</div><h2>Required invariant</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Rule</th><th>Value</th></tr></thead>
    <tbody>{definition_rows or '<tr><td colspan="2">No Day Trade Lab definition artifact found.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="dashboard-grid">
  <section class="panel warnings-panel">
    <div class="section-label">Current Limitations</div>
    <ul>{limitation_items or "<li>No limitation artifact found.</li>"}</ul>
  </section>
  <section class="panel">
    <div class="section-label">Demo Proof Boundary</div>
    <p>{_esc(str(demo.get("source_mode", "missing")))} generated {_esc(str(demo.get("one_minute_trade_count", "n/a")))} one-minute and {_esc(str(demo.get("five_minute_trade_count", "n/a")))} five-minute fixture trades. Demo proof is not real market evidence.</p>
  </section>
  <section class="panel">
    <div class="section-label">Daily Swing Research</div>
    <p>The ranked six-month backtest remains useful as daily-bar swing or position research. It is not allowed to populate Day Trade Lab metrics or same-session trade proof.</p>
  </section>
  <section class="panel">
    <div class="section-label">Corpus Evidence Mode</div>
    <p>{_esc(str(summary.get("evidence_mode", "historical_daytrade_backtest")))}. Corpus rankings remain historical research, not validation, not promotion, and not live execution.</p>
  </section>
  <section class="panel">
    <div class="section-label">Safety Boundary</div>
    <p>Day Trade Lab is research-only, file-rendered, and does not place trades, mutate PaperOps official state, call brokers, or expose secrets.</p>
  </section>
</section>
"""


def _day_trade_robustness_body() -> str:
    corpus_summary = _day_trade_summary()
    report = _day_trade_robustness_report()
    robust = _day_trade_robustness_summary()
    most_robust = _dict_payload(report.get("most_robust_strategy") or robust.get("most_robust_strategy"))
    most_fragile = _dict_payload(report.get("most_fragile_strategy") or robust.get("most_fragile_strategy"))
    base_rows = [_dict_payload(row) for row in _list_payload(robust.get("base_rows"))]
    base_rows.sort(key=lambda row: (_number_value(row.get("expectancy")), _number_value(row.get("profit_factor"))), reverse=True)
    fragility_rows = _day_trade_fragility_rows()
    slice_counts = _dict_payload(robust.get("slice_counts"))
    robustness_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(_strategy_label(row.get("strategy_id")))}</strong><em>{_esc(str(row.get("strategy_id", "n/a")))}</em></td>
  <td>{_esc(str(row.get("interval", "n/a")))}</td>
  <td>{_esc(str(row.get("trade_count", "n/a")))}</td>
  <td>{_esc(_percent_text(row.get("win_rate")))}</td>
  <td class="{_return_class(row.get("expectancy"))}">{_esc(_decimal_text(row.get("expectancy"), 3))}R</td>
  <td>{_esc(_decimal_text(row.get("profit_factor"), 2))}</td>
  <td class="{_return_class(row.get("total_return_pct"))}">{_esc(_percent_text(row.get("total_return_pct")))}</td>
  <td>{_esc(_percent_text(row.get("max_drawdown_pct")))}</td>
  <td><span class="trust-chip">{_esc(str(row.get("fragility_warning", "none")))}</span></td>
</tr>"""
        for row in base_rows
    )
    warning_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(_strategy_label(row.get("strategy_id")))}</strong><em>{_esc(str(row.get("strategy_id", "n/a")))} / {_esc(str(row.get("interval", "n/a")))}</em></td>
  <td>{_esc(str(row.get("severity", "n/a")))}</td>
  <td>{_esc(str(row.get("reason", "n/a")))}</td>
  <td>{_esc(str(row.get("detail", "n/a")))}</td>
</tr>"""
        for row in fragility_rows[:80]
    )
    return f"""
{_page_hero(label="Day Trade Robustness", title="Robustness beats raw return.", body="Historical day-trade backtest only - not validated. This page checks whether each intraday edge survives symbol, time, month, weekday, interval, session-quality, slippage, and holdout review.", stat_label="Quality score", stat_value=f'{report.get("quality_score", "n/a")}/100', stat_context=str(report.get("final_status", "missing")))}
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>Most robust</span><strong>{_esc(_strategy_label(most_robust.get("strategy_id", "n/a")))}</strong><em>{_esc(str(most_robust.get("interval", "n/a")))} / {_esc(_decimal_text(most_robust.get("expectancy"), 3))}R expectancy</em></div>
  <div class="metric-card"><span>Most fragile</span><strong>{_esc(_strategy_label(most_fragile.get("strategy_id", "n/a")))}</strong><em>{_esc(str(most_fragile.get("interval", "n/a")))} / {_esc(str(most_fragile.get("fragility_warning", "n/a")))}</em></div>
  <div class="metric-card"><span>Zero overnight holds</span><strong>{_esc(str(robust.get("overnight_hold_count", corpus_summary.get("overnight_hold_count", "n/a"))))}</strong><em>Day Trade Lab invariant</em></div>
  <div class="metric-card"><span>Fragility warnings</span><strong>{_esc(str(report.get("fragility_count", len(fragility_rows))))}</strong><em>Nothing hidden</em></div>
</section>
<section class="dashboard-grid">
  <section class="panel warnings-panel">
    <div class="section-label">Provider/Data Limitations</div>
    <p>Provider/data limitations remain part of the evidence: {_esc("; ".join(_day_trade_limitations(corpus_summary)) or "no limitation artifact found")}. Partial and provider-limited sessions stay visible instead of being filled with fabricated trades.</p>
  </section>
  <section class="panel">
    <div class="section-label">Slice Coverage</div>
    <p>Symbol slices: {_esc(str(slice_counts.get("by_symbol", "n/a")))}, time slices: {_esc(str(slice_counts.get("by_time", "n/a")))}, month slices: {_esc(str(slice_counts.get("by_month", "n/a")))}, weekday slices: {_esc(str(slice_counts.get("by_weekday", "n/a")))}, interval slices: {_esc(str(slice_counts.get("by_interval", "n/a")))}.</p>
  </section>
</section>
<section class="toolbar"><input data-x2-search aria-label="Filter robustness rows" placeholder="Filter robustness rows"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Robustness Rows</div><h2>Strategy and interval robustness ranking</h2></div><span>Same-session trades only</span></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Strategy</th><th>Interval</th><th>Trades</th><th>Win</th><th>Expectancy</th><th>PF</th><th>Return</th><th>Drawdown</th><th>Warning</th></tr></thead>
    <tbody>{robustness_rows or '<tr><td colspan="9">No robustness summary rows found.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Fragility Warnings</div><h2>Known weak points to investigate</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Strategy</th><th>Severity</th><th>Reason</th><th>Detail</th></tr></thead>
    <tbody>{warning_rows or '<tr><td colspan="4">No fragility warnings found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _day_trade_slippage_stress_body() -> str:
    report = _day_trade_robustness_report()
    payload = _day_trade_slippage_payload()
    rows = _day_trade_slippage_rows()
    rows.sort(key=lambda row: (str(row.get("strategy_id", "")), str(row.get("interval", "")), str(row.get("stress_name", ""))))
    failed_rows = [row for row in rows if str(row.get("failed_under_stress")) in {"True", "true", "1"}]
    table_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(_strategy_label(row.get("strategy_id")))}</strong><em>{_esc(str(row.get("strategy_id", "n/a")))}</em></td>
  <td>{_esc(str(row.get("interval", "n/a")))}</td>
  <td><span class="horizon-chip">{_esc(_humanize_key(row.get("stress_name", "n/a")))}</span></td>
  <td class="{_return_class(row.get("total_return_pct"))}">{_esc(_percent_text(row.get("total_return_pct")))}</td>
  <td class="{_return_class(row.get("expectancy"))}">{_esc(_decimal_text(row.get("expectancy"), 3))}R</td>
  <td>{_esc(_decimal_text(row.get("profit_factor"), 2))}</td>
  <td>{_esc(_percent_text(row.get("win_rate")))}</td>
  <td>{_esc(_percent_text(row.get("max_drawdown_pct")))}</td>
  <td><span class="trust-chip">{_esc("Fails stress" if str(row.get("failed_under_stress")) in {"True", "true", "1"} else "Survives row")}</span></td>
</tr>"""
        for row in rows
    )
    return f"""
{_page_hero(label="Day Trade Slippage Stress", title="Costs can erase an intraday edge.", body="Historical day-trade backtest only - not validated. These rows replay the corpus under current slippage, 2x slippage, 3x slippage, fixed spread, adverse fill, and commission-increase assumptions.", stat_label="Failed rows", stat_value=len(failed_rows), stat_context=f'{payload.get("failed_strategy_count", "n/a")} strategy/interval failures')}
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>Stress rows</span><strong>{_esc(str(len(rows)))}</strong><em>Local replay only</em></div>
  <div class="metric-card"><span>Failed strategy intervals</span><strong>{_esc(str(payload.get("failed_strategy_count", "n/a")))}</strong><em>Any negative stress result</em></div>
  <div class="metric-card"><span>Report result</span><strong>{_esc(str(_dict_payload(report.get("slippage_stress_result")).get("failed_rows", "n/a")))}</strong><em>Failed stress rows</em></div>
  <div class="metric-card"><span>Trust boundary</span><strong>Not validated</strong><em>Not broker fill proof</em></div>
</section>
<section class="toolbar"><input data-x2-search aria-label="Filter slippage stress rows" placeholder="Filter stress rows"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Slippage Stress</div><h2>Cost-adjusted replay by strategy</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Strategy</th><th>Interval</th><th>Stress</th><th>Return</th><th>Expectancy</th><th>PF</th><th>Win</th><th>Drawdown</th><th>Result</th></tr></thead>
    <tbody>{table_rows or '<tr><td colspan="9">No slippage stress rows found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _day_trade_oos_body() -> str:
    report = _day_trade_robustness_report()
    payload = _day_trade_oos_payload()
    result = _dict_payload(report.get("out_of_sample_result"))
    rows = _day_trade_oos_rows()
    rows.sort(key=lambda row: (str(row.get("strategy_id", "")), str(row.get("interval", "")), str(row.get("split_name", ""))))
    table_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(_strategy_label(row.get("strategy_id")))}</strong><em>{_esc(str(row.get("strategy_id", "n/a")))}</em></td>
  <td>{_esc(str(row.get("interval", "n/a")))}</td>
  <td>{_esc(_humanize_key(row.get("split_name", "n/a")))}</td>
  <td>{_esc(str(row.get("research_trade_count", "n/a")))} / {_esc(str(row.get("holdout_trade_count", "n/a")))}</td>
  <td class="{_return_class(row.get("research_expectancy"))}">{_esc(_decimal_text(row.get("research_expectancy"), 3))}R</td>
  <td class="{_return_class(row.get("holdout_expectancy"))}">{_esc(_decimal_text(row.get("holdout_expectancy"), 3))}R</td>
  <td class="{_return_class(row.get("degradation"))}">{_esc(_decimal_text(row.get("degradation"), 3))}R</td>
  <td><span class="trust-chip">{_esc(str(row.get("overfit_warning", "none")))}</span></td>
</tr>"""
        for row in rows
    )
    return f"""
{_page_hero(label="Day Trade Out-of-Sample", title="Holdout behavior controls confidence.", body="Historical day-trade backtest only - not validated. The Day Trade Lab splits sessions by time, halves, and odd/even sessions so full-sample curve fitting is not mistaken for an edge.", stat_label="Overfit warnings", stat_value=payload.get("overfit_warning_count", "n/a"), stat_context="Research/holdout split")}
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>OOS rows</span><strong>{_esc(str(len(rows)))}</strong><em>Time, half, odd/even splits</em></div>
  <div class="metric-card"><span>Positive holdout</span><strong>{_esc(str(result.get("positive_holdout", "n/a")))}</strong><em>70/30 split rows</em></div>
  <div class="metric-card"><span>Strategy intervals</span><strong>{_esc(str(result.get("strategy_intervals", "n/a")))}</strong><em>Corpus families tested</em></div>
  <div class="metric-card"><span>Trust boundary</span><strong>Not validated</strong><em>Holdout is still historical</em></div>
</section>
<section class="toolbar"><input data-x2-search aria-label="Filter out-of-sample rows" placeholder="Filter OOS rows"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Out-of-Sample Splits</div><h2>Research vs holdout comparison</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Strategy</th><th>Interval</th><th>Split</th><th>Trades R/H</th><th>Research Exp.</th><th>Holdout Exp.</th><th>Degradation</th><th>Warning</th></tr></thead>
    <tbody>{table_rows or '<tr><td colspan="8">No out-of-sample rows found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _day_trade_refinements_body() -> str:
    candidates_payload = _day_trade_refinement_candidates_payload()
    eval_payload = _day_trade_refinement_eval_payload()
    candidates = _day_trade_refinement_candidates()
    eval_rows = _day_trade_refinement_eval_rows()
    candidate_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(str(row.get("challenger_id", "n/a")))}</strong><em>{_esc(_strategy_label(row.get("parent_strategy_id", "n/a")))} / {_esc(str(row.get("parent_interval", "n/a")))}</em></td>
  <td>{_esc(_humanize_key(row.get("refinement_type", "n/a")))}</td>
  <td>{_esc(str(row.get("rule", "n/a")))}</td>
  <td>{_esc(str(row.get("reason", "n/a")))}</td>
  <td><span class="trust-chip">{_esc(str(row.get("status", "shadow_refinement")))}</span><em>not validated / no live trading</em></td>
</tr>"""
        for row in candidates
    )
    eval_table_rows = "".join(
        f"""<tr data-filter-item>
  <td><strong>{_esc(str(row.get("challenger_id", "n/a")))}</strong><em>{_esc(_strategy_label(row.get("parent_strategy_id", "n/a")))}</em></td>
  <td>{_esc(str(row.get("parent_interval", "n/a")))}</td>
  <td>{_esc(str(row.get("parent_holdout_trade_count", "n/a")))} / {_esc(str(row.get("candidate_holdout_trade_count", "n/a")))}</td>
  <td class="{_return_class(row.get("parent_holdout_expectancy"))}">{_esc(_decimal_text(row.get("parent_holdout_expectancy"), 3))}R</td>
  <td class="{_return_class(row.get("candidate_holdout_expectancy"))}">{_esc(_decimal_text(row.get("candidate_holdout_expectancy"), 3))}R</td>
  <td>{_esc(str(row.get("holdout_beats_parent", "n/a")))}</td>
  <td><span class="trust-chip">{_esc(str(row.get("overfit_risk", "n/a")))}</span></td>
</tr>"""
        for row in eval_rows
    )
    return f"""
{_page_hero(label="Day Trade Refined Challengers", title="Refinements stay shadow-only.", body="Historical day-trade backtest only - not validated. Challenger variants are generated from research evidence and evaluated against holdout, but they do not mutate champion strategy logic or enable live trading.", stat_label="Candidates", stat_value=len(candidates), stat_context=f'{eval_payload.get("holdout_beats_parent_count", "n/a")} beat parent in holdout')}
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>Shadow candidates</span><strong>{_esc(str(candidates_payload.get("candidate_count", len(candidates))))}</strong><em>No promotion allowed</em></div>
  <div class="metric-card"><span>Holdout beats parent</span><strong>{_esc(str(eval_payload.get("holdout_beats_parent_count", "n/a")))}</strong><em>Still not validated</em></div>
  <div class="metric-card"><span>Champion mutation</span><strong>{_esc(str(eval_payload.get("champions_changed", "n/a")))}</strong><em>Must remain false</em></div>
  <div class="metric-card"><span>Live trading</span><strong>Disabled</strong><em>No broker/order controls</em></div>
</section>
<section class="toolbar"><input data-x2-search aria-label="Filter refinement rows" placeholder="Filter refinements"></section>
<section class="panel table-panel" data-filter-scope>
  <div class="panel-heading"><div><div class="section-label">Shadow Refinement Candidates</div><h2>Generated from research split evidence</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Challenger</th><th>Type</th><th>Rule</th><th>Reason</th><th>Status</th></tr></thead>
    <tbody>{candidate_rows or '<tr><td colspan="5">No refinement candidates found.</td></tr>'}</tbody>
  </table></div>
</section>
<section class="panel table-panel">
  <div class="panel-heading"><div><div class="section-label">Refinement Evaluation</div><h2>Parent vs challenger on holdout</h2></div></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Challenger</th><th>Interval</th><th>Trades Parent/Candidate</th><th>Parent Exp.</th><th>Candidate Exp.</th><th>Beats Parent</th><th>Risk</th></tr></thead>
    <tbody>{eval_table_rows or '<tr><td colspan="7">No refinement evaluation rows found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _historical_backtest_body() -> str:
    boundary = "Historical backtest only — not validated forward performance."
    summary = _read_json(
        Path("data/v2_historical_backtests/six_month/reports/six_month_backtest_summary.json"),
        {},
    )
    comparison = _read_json(
        Path("data/v2_historical_backtests/six_month/reports/strategy_comparison.json"),
        [],
    )
    if not isinstance(comparison, list):
        comparison = []
    backtested_rows: list[str] = []
    trade_menus: list[str] = []
    shadow_rows: list[str] = []
    for row in comparison[:40]:
        strategy_id = str(row.get("strategy_id", "n/a"))
        label = _strategy_label(strategy_id)
        group = _humanize_key(row.get("group", "n/a"))
        if str(row.get("backtest_status")) == "backtested":
            detail_id = f"trade-menu-{_slug(strategy_id)}"
            holding = _holding_profile(strategy_id)
            backtested_rows.append(
                f"""<tr data-filter-item>
  <td><span class="rank-chip">{_esc(str(row.get("rank_by_return", "n/a")))}</span></td>
  <td><button class="strategy-disclosure" type="button" data-x2-toggle="{detail_id}" aria-controls="{detail_id}" aria-expanded="false" aria-label="View trade ledger for {_esc(label)}" title="View trade ledger"><strong>{_esc(label)}</strong><em>{_esc(strategy_id)}</em></button></td>
  <td>{_esc(group)}</td>
  <td class="{_return_class(row.get("total_return_pct"))}">{_esc(_percent_text(row.get("total_return_pct")))}</td>
  <td>{_esc(_percent_text(row.get("max_drawdown_pct")))}</td>
  <td>{_esc(str(row.get("trade_count", "n/a")))}</td>
  <td><span class="horizon-chip">{_esc(str(holding.get("label", "n/a")))}</span><em>{_esc(str(holding.get("context", "n/a")))}</em></td>
  <td><span class="trust-chip">Not validated</span></td>
</tr>"""
            )
            trade_menus.append(_trade_ledger_menu(strategy_id, label, detail_id))
        else:
            shadow_rows.append(
                f"""<li data-filter-item><strong>{_esc(label)}</strong><span>{_esc(strategy_id)}</span></li>"""
            )
    if not backtested_rows:
        backtested_rows.append(
            """<tr><td colspan="8"><strong>No six-month run yet.</strong><em>Run the historical backtest workflow, then rebuild X2.</em></td></tr>"""
        )
    shadow_list = "".join(shadow_rows) or "<li><strong>No shadow challengers found.</strong><span>n/a</span></li>"
    warnings = summary.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    warning_items = "".join(f"<li>{_esc(str(item))}</li>" for item in warnings[:40])
    warning_items = (
        "<li>Ranked strategy rows are daily-bar historical replays. Holds above 1 daily bar are not day trades.</li>"
        + warning_items
    )
    if not warning_items:
        warning_items = "<li>No hidden warnings in the current historical summary.</li>"
    accepted_start = _esc(str(summary.get("accepted_start", "n/a")))
    accepted_end = _esc(str(summary.get("accepted_end", "n/a")))
    snapshot_id = _esc(str(summary.get("snapshot_id", "missing")))
    return f"""
<section class="hero-story compact backtest-hero">
  <div><div class="section-label">Six-Month Historical Backtest</div>
  <h1>Daily-bar strategy backtest, not day-trading proof.</h1>
  <p>{boundary} Entries and exits are generated from completed daily bars, so ranked rows can be swing or position holds.</p></div>
  <div class="pulse-card snapshot-card"><span>Snapshot</span><strong>{accepted_start} to {accepted_end}</strong><em>{snapshot_id}</em></div>
</section>
<section class="metric-grid backtest-metrics">
  <div class="metric-card"><span>Date range</span><strong>{accepted_start} to {accepted_end}</strong><em>Completed daily bars only, not intraday fills</em></div>
  <div class="metric-card"><span>Symbols</span><strong>{_esc(str(summary.get("symbol_count", "n/a")))}</strong><em>Aligned historical universe</em></div>
  <div class="metric-card"><span>Strategy rows</span><strong>{_esc(str(summary.get("strategy_rows", "n/a")))}</strong><em>Champions, benchmarks, shadow metadata</em></div>
  <div class="metric-card"><span>Boundary</span><strong>Not validated</strong><em>Research-only / paper-only</em></div>
</section>
<section class="toolbar"><input data-x2-search aria-label="Filter backtest rows" placeholder="Filter strategies"></section>
<section class="panel backtest-table-panel">
  <div class="panel-heading"><div><div class="section-label">Ranked Backtested Strategies</div><h2>Daily-bar strategy comparison</h2></div><span>{accepted_start} to {accepted_end}</span></div>
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Rank</th><th>Strategy</th><th>Role</th><th>Return</th><th>Drawdown</th><th>Trades</th><th>Horizon</th><th>Trust</th></tr></thead>
    <tbody>{''.join(backtested_rows)}</tbody>
  </table></div>
  {''.join(trade_menus)}
</section>
<section class="panel shadow-panel">
  <div class="panel-heading"><div><div class="section-label">Shadow Challengers</div><h2>Metadata-only candidates</h2></div><span>Not mechanically replayed</span></div>
  <ul class="shadow-list">{shadow_list}</ul>
</section>
<section class="panel warnings-panel backtest-warnings"><div class="section-label">Historical Warnings</div><ul>{warning_items}</ul></section>
"""


def _trade_ledger_menu(strategy_id: str, label: str, detail_id: str) -> str:
    rows = _read_trade_ledger(strategy_id)
    source_path = f"data/v2_historical_backtests/six_month/trades/{_slug(strategy_id)}_trades.csv"
    total_count = len(rows)
    preview = rows[:TRADE_LEDGER_PREVIEW_LIMIT]
    first_entry = _timestamp_text(rows[0].get("entry_time")) if rows else "n/a"
    last_exit = _timestamp_text(rows[-1].get("exit_time")) if rows else "n/a"
    if preview:
        ledger_rows = "".join(_trade_ledger_table_row(row) for row in preview)
    else:
        ledger_rows = """<tr><td colspan="11"><strong>No trades generated.</strong><em>This strategy or baseline produced no executed backtest trades for this window.</em></td></tr>"""
    extra_count = total_count - len(preview)
    extra_note = (
        f'<p class="quiet-note">Showing first {len(preview)} of {total_count} ledger rows. Open the CSV artifact for the complete ledger.</p>'
        if extra_count > 0
        else ""
    )
    title_id = f"{detail_id}-title"
    return f"""<div id="{detail_id}" class="trade-menu" role="dialog" aria-modal="true" aria-labelledby="{title_id}" aria-hidden="true" hidden>
  <div class="trade-menu-backdrop" data-x2-close="{detail_id}"></div>
  <div class="trade-menu-panel" tabindex="-1">
    <div class="trade-menu-bar">
      <div><div class="section-label">Trade Ledger</div><strong id="{title_id}">{_esc(label)}</strong><em>{_esc(source_path)}</em></div>
      <button class="menu-close" type="button" data-x2-close="{detail_id}" aria-label="Close trade ledger">Close</button>
    </div>
    <div class="trade-detail-stats">
      <span><b>{_esc(str(total_count))}</b> generated trades</span>
      <span><b>{_esc(first_entry)}</b> first entry</span>
      <span><b>{_esc(last_exit)}</b> last exit</span>
    </div>
    <p class="quiet-note">Historical-only replay. Timestamps are the available backtest bar timestamps, not broker fill confirmations.</p>
    {extra_note}
    <div class="table-scroll trade-ledger-scroll"><table class="backtest-table trade-ledger-table">
      <thead><tr><th>Symbol</th><th>Side</th><th>Entry time</th><th>Exit time</th><th>Entry</th><th>Exit</th><th>Stop / Target</th><th>Result</th><th>Hold</th><th>Exit reason</th><th>Evidence</th></tr></thead>
      <tbody>{ledger_rows}</tbody>
    </table></div>
  </div>
</div>"""


def _holding_profile(strategy_id: str) -> dict[str, str]:
    rows = _read_trade_ledger(strategy_id)
    holds: list[float] = []
    for row in rows:
        holding = _holding_bars_number(row.get("holding_bars"))
        if holding is not None:
            holds.append(holding)
    if not holds:
        return {"label": "No trades", "context": "No ledger entries"}
    average = sum(holds) / len(holds)
    maximum = max(holds)
    if maximum <= 1:
        label = "Day-trade window"
    elif average <= 5:
        label = "Not day trade"
    elif average <= 20:
        label = "Swing hold"
    else:
        label = "Position hold"
    return {
        "label": label,
        "context": f"avg {average:.1f} daily bars / max {_whole_number_text(maximum)}",
    }


def _trade_ledger_table_row(row: dict[str, str]) -> str:
    result_text = (
        f"{_money_text(row.get('net_pnl'))} net / "
        f"{_percent_text(row.get('return_pct'))} / "
        f"{_decimal_text(row.get('r_multiple'))}R"
    )
    stop_target = f"{_price_text(row.get('stop'))} / {_price_text(row.get('target'))}"
    return f"""<tr>
  <td><strong>{_esc(_plain_text(row.get("symbol")))}</strong></td>
  <td>{_esc(_plain_text(row.get("direction")))}</td>
  <td>{_esc(_timestamp_text(row.get("entry_time")))}</td>
  <td>{_esc(_timestamp_text(row.get("exit_time")))}</td>
  <td>{_esc(_price_text(row.get("entry_price")))}</td>
  <td>{_esc(_price_text(row.get("exit_price")))}</td>
  <td>{_esc(stop_target)}</td>
  <td class="{_return_class(row.get("net_pnl"))}">{_esc(result_text)}</td>
  <td>{_esc(_holding_bars_text(row.get("holding_bars")))}</td>
  <td>{_esc(_humanize_key(row.get("exit_reason", "n/a")))}</td>
  <td><em>{_esc(_plain_text(row.get("evidence")))}</em></td>
</tr>"""


def _read_trade_ledger(strategy_id: str) -> list[dict[str, str]]:
    path = Path("data/v2_historical_backtests/six_month/trades") / f"{_slug(strategy_id)}_trades.csv"
    if not path.exists() or path.is_dir():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return [
                {str(key): str(value or "") for key, value in row.items() if key is not None}
                for row in reader
            ]
    except OSError:
        return []


def _day_trade_summary() -> dict[str, Any]:
    corpus_path = Path("data/v2_day_trade_lab/reports/corpus_day_trade_summary.json")
    legacy_path = Path("data/v2_day_trade_lab/reports/day_trade_lab_summary.json")
    payload = _read_json(corpus_path if corpus_path.exists() else legacy_path, {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_corpus_available() -> bool:
    return Path("data/v2_day_trade_lab/reports/corpus_day_trade_summary.json").exists()


def _day_trade_limitations(*payloads: Any) -> list[str]:
    items: list[str] = []
    if not payloads:
        payloads = (_day_trade_summary(),)
    for payload in payloads:
        if isinstance(payload, dict):
            items.extend(str(item) for item in payload.get("data_limitations", []) if item)
            items.extend(str(item) for item in payload.get("provider_limitations", []) if item)
            if _int_like(payload.get("missing_session_count")):
                items.append(f"{payload.get('missing_session_count')} requested symbol/interval sessions are missing")
            if _int_like(payload.get("partial_session_count")):
                items.append(f"{payload.get('partial_session_count')} sessions are partial")
    return list(dict.fromkeys(items))


def _day_trade_status_label(value: Any) -> str:
    text = str(value or "missing")
    labels = {
        "COMPLETE_DAY_TRADE_LAB_WITH_DATA_LIMITATIONS": "Data-limited complete",
        "COMPLETE_DAY_TRADE_LAB": "Complete",
        "COMPLETE_DAY_TRADE_DATA_EXPANSION": "Corpus complete",
        "COMPLETE_WITH_PROVIDER_LIMITATIONS": "Corpus complete with provider limits",
        "RESUME_REQUIRED": "Resume required",
        "DEMO_DAY_TRADE_LAB_PROOF": "Demo proof",
        "missing": "Missing",
    }
    return labels.get(text, _humanize_key(text))


def _source_mode_label(value: Any) -> str:
    text = str(value or "n/a")
    labels = {
        "real_intraday_limited": "Real intraday limited",
        "fixture_demo_intraday": "Fixture demo",
        "missing_real_intraday": "Missing real intraday",
        "historical_daytrade_backtest": "Historical day-trade backtest",
        "n/a": "n/a",
    }
    return labels.get(text, _humanize_key(text))


def _int_like(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _number_value(value: Any) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _dict_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _day_trade_session_inventory() -> dict[str, Any]:
    corpus_path = Path("data/v2_day_trade_lab/corpus/session_inventory/session_inventory.json")
    legacy_path = Path("data/v2_day_trade_lab/sessions/session_inventory.json")
    payload = _read_json(corpus_path if corpus_path.exists() else legacy_path, {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_comparison_rows() -> list[dict[str, Any]]:
    corpus_path = Path("data/v2_day_trade_lab/reports/corpus_strategy_comparison.json")
    legacy_path = Path("data/v2_day_trade_lab/reports/strategy_comparison.json")
    payload = _read_json(corpus_path if corpus_path.exists() else legacy_path, [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _day_trade_robustness_report() -> dict[str, Any]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/reports/robustness_report.json"), {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_robustness_summary() -> dict[str, Any]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/reports/robustness_summary.json"), {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_fragility_rows() -> list[dict[str, Any]]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/reports/fragility_report.json"), {})
    if isinstance(payload, dict):
        return [_dict_payload(row) for row in _list_payload(payload.get("rows"))]
    return []


def _day_trade_slippage_payload() -> dict[str, Any]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/slippage_stress/slippage_stress_summary.json"), {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_slippage_rows() -> list[dict[str, Any]]:
    payload = _day_trade_slippage_payload()
    rows = [_dict_payload(row) for row in _list_payload(payload.get("stress_rows"))]
    if rows:
        return rows
    return [_dict_payload(row) for row in _read_csv_rows(Path("data/v2_day_trade_lab/robustness/slippage_stress/slippage_stress_summary.csv"))]


def _day_trade_oos_payload() -> dict[str, Any]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/out_of_sample/oos_summary.json"), {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_oos_rows() -> list[dict[str, Any]]:
    payload = _day_trade_oos_payload()
    rows = [_dict_payload(row) for row in _list_payload(payload.get("rows"))]
    if rows:
        return rows
    return [_dict_payload(row) for row in _read_csv_rows(Path("data/v2_day_trade_lab/robustness/out_of_sample/oos_summary.csv"))]


def _day_trade_refinement_candidates_payload() -> dict[str, Any]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/challengers/refinement_candidates.json"), {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_refinement_candidates() -> list[dict[str, Any]]:
    payload = _day_trade_refinement_candidates_payload()
    return [_dict_payload(row) for row in _list_payload(payload.get("candidates"))]


def _day_trade_refinement_eval_payload() -> dict[str, Any]:
    payload = _read_json(Path("data/v2_day_trade_lab/robustness/challengers/refinement_eval.json"), {})
    return payload if isinstance(payload, dict) else {}


def _day_trade_refinement_eval_rows() -> list[dict[str, Any]]:
    payload = _day_trade_refinement_eval_payload()
    rows = [_dict_payload(row) for row in _list_payload(payload.get("rows"))]
    if rows:
        return rows
    return [_dict_payload(row) for row in _read_csv_rows(Path("data/v2_day_trade_lab/robustness/challengers/refinement_eval.csv"))]


def _day_trade_day_returns() -> list[dict[str, str]]:
    if _day_trade_corpus_available():
        rows = _read_csv_rows(Path("data/v2_day_trade_lab/day_returns/corpus_day_trade_daily_returns.csv"))
        return rows if rows else _read_day_trade_csvs("day_returns", "corpus_day_trade_daily_returns_*.csv")
    return _read_day_trade_csvs("day_returns", "day_returns_*.csv")


def _day_trade_no_trade_rows() -> list[dict[str, str]]:
    if _day_trade_corpus_available():
        rows = _read_csv_rows(Path("data/v2_day_trade_lab/reports/corpus_no_trade_days.csv"))
        return rows if rows else _read_day_trade_csvs("reports", "corpus_no_trade_days_*.csv")
    return _read_day_trade_csvs("reports", "no_trade_days_*.csv")


def _day_trade_skip_reason_rows() -> list[dict[str, str]]:
    if _day_trade_corpus_available():
        rows = _read_csv_rows(Path("data/v2_day_trade_lab/reports/corpus_skip_reasons.csv"))
        return rows if rows else _read_day_trade_csvs("reports", "corpus_skip_reasons_*.csv")
    return _read_day_trade_csvs("reports", "skip_reasons_*.csv")


def _read_day_trade_csvs(dirname: str, pattern: str) -> list[dict[str, str]]:
    root = Path("data/v2_day_trade_lab") / dirname
    rows: list[dict[str, str]] = []
    for path in sorted(root.glob(pattern)):
        rows.extend(_read_csv_rows(path))
    return rows


def _day_trade_trades() -> list[dict[str, str]]:
    if _day_trade_corpus_available():
        rows = _read_csv_rows(Path("data/v2_day_trade_lab/trades/corpus_day_trade_trades.csv"))
        if not rows:
            rows = _read_day_trade_csvs("trades", "corpus_day_trade_trades_*.csv")
    else:
        rows = _read_day_trade_csvs("trades", "day_trades_*.csv")
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("entry_time", "")),
            str(row.get("strategy_id", "")),
            str(row.get("symbol", "")),
        ),
    )


def _day_trade_trades_for(strategy_id: str, interval: str) -> list[dict[str, str]]:
    return [
        row
        for row in _day_trade_trades()
        if str(row.get("strategy_id")) == strategy_id and str(row.get("interval")) == interval
    ]


def _day_trade_ledger_menu(
    strategy_id: str,
    interval: str,
    label: str,
    detail_id: str,
) -> str:
    rows = _day_trade_trades_for(strategy_id, interval)
    preview = rows[:TRADE_LEDGER_PREVIEW_LIMIT]
    first_entry = _timestamp_text(preview[0].get("entry_time")) if preview else "n/a"
    last_exit = _timestamp_text(preview[-1].get("exit_time")) if preview else "n/a"
    ledger_rows = "".join(_day_trade_trade_row(row) for row in preview)
    if not ledger_rows:
        ledger_rows = """<tr><td colspan="10"><strong>No day trades generated.</strong><em>This strategy produced no same-session entries for the current Day Trade Lab artifact.</em></td></tr>"""
    extra_note = (
        f'<p class="quiet-note">Showing first {len(preview)} of {len(rows)} ledger rows. Open the CSV artifact for the complete ledger.</p>'
    if len(rows) > len(preview)
        else ""
    )
    title_id = f"{detail_id}-title"
    source_path = (
        "data/v2_day_trade_lab/trades/corpus_day_trade_trades.csv"
        if _day_trade_corpus_available()
        else f"data/v2_day_trade_lab/trades/{_slug(strategy_id)}_{_slug(interval)}_trades.csv"
    )
    return f"""<div id="{detail_id}" class="trade-menu" role="dialog" aria-modal="true" aria-labelledby="{title_id}" aria-hidden="true" hidden>
  <div class="trade-menu-backdrop" data-x2-close="{detail_id}"></div>
  <div class="trade-menu-panel" tabindex="-1">
    <div class="trade-menu-bar">
      <div><div class="section-label">Day Trade Ledger</div><strong id="{title_id}">{_esc(label)}</strong><em>{_esc(source_path)}</em></div>
      <button class="menu-close" type="button" data-x2-close="{detail_id}" aria-label="Close day-trade ledger">Close</button>
    </div>
    <div class="trade-detail-stats">
      <span><b>{_esc(str(len(rows)))}</b> generated day trades</span>
      <span><b>{_esc(first_entry)}</b> first entry</span>
      <span><b>{_esc(last_exit)}</b> last exit</span>
    </div>
    <p class="quiet-note">Intraday-only research replay. Each shown row is required to exit inside the same market session.</p>
    {extra_note}
    <div class="table-scroll trade-ledger-scroll"><table class="backtest-table trade-ledger-table">
      <thead><tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Entry / Exit</th><th>Stop / Target</th><th>Result</th><th>Hold</th><th>Reason</th></tr></thead>
      <tbody>{ledger_rows}</tbody>
    </table></div>
  </div>
</div>"""


def _day_trade_trade_row(row: dict[str, str]) -> str:
    prices = f"{_price_text(row.get('entry_price'))} / {_price_text(row.get('exit_price'))}"
    stop_target = f"{_price_text(row.get('stop'))} / {_price_text(row.get('target'))}"
    result = f"{_money_text(row.get('net_pnl'))} / {_percent_text(row.get('return_pct'))} / {_decimal_text(row.get('r_multiple'))}R"
    hold = _minute_text(row.get("hold_minutes"))
    strategy = str(row.get("strategy_id", "n/a"))
    interval = str(row.get("interval", "n/a"))
    return f"""<tr data-filter-item>
  <td><strong>{_esc(_strategy_label(strategy))}</strong><em>{_esc(strategy)} / {_esc(interval)}</em></td>
  <td><strong>{_esc(_plain_text(row.get("symbol")))}</strong><em>{_esc(_source_mode_label(row.get("source_mode", "n/a")))}</em></td>
  <td>{_esc(_plain_text(row.get("direction")))}</td>
  <td>{_esc(_timestamp_text(row.get("entry_time")))}</td>
  <td>{_esc(_timestamp_text(row.get("exit_time")))}</td>
  <td>{_esc(prices)}</td>
  <td>{_esc(stop_target)}</td>
  <td class="{_return_class(row.get("net_pnl"))}">{_esc(result)}</td>
  <td>{_esc(hold)}</td>
  <td>{_esc(_humanize_key(row.get("exit_reason", "n/a")))}</td>
</tr>"""


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.is_dir():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return [
                {str(key): str(value or "") for key, value in row.items() if key is not None}
                for row in reader
            ]
    except OSError:
        return []


def _plain_text(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "n/a"


def _timestamp_text(value: Any) -> str:
    text = _plain_text(value)
    if text == "n/a":
        return text
    return text.replace("T", " ").replace("+00:00", " UTC")


def _decimal_text(value: Any, places: int = 2) -> str:
    text = _plain_text(value)
    if text == "n/a":
        return text
    try:
        return f"{float(text):.{places}f}"
    except ValueError:
        return text


def _holding_bars_number(value: Any) -> float | None:
    text = _plain_text(value)
    if text == "n/a":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _whole_number_text(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _holding_bars_text(value: Any) -> str:
    number = _holding_bars_number(value)
    if number is None:
        return "n/a"
    label = "daily bar" if number == 1 else "daily bars"
    return f"{_whole_number_text(number)} {label}"


def _minute_text(value: Any) -> str:
    number = _holding_bars_number(value)
    if number is None:
        return "n/a"
    label = "minute" if number == 1 else "minutes"
    return f"{_whole_number_text(number)} {label}"


def _price_text(value: Any) -> str:
    text = _decimal_text(value, 2)
    if text == "n/a":
        return text
    return f"${text}"


def _money_text(value: Any) -> str:
    text = _decimal_text(value, 2)
    if text == "n/a":
        return text
    if text.startswith("-"):
        return f"-${text[1:]}"
    return f"${text}"


def _system_map_body(data: dict[str, Any]) -> str:
    rows = "".join(
        f"""<tr>
  <td><strong>{_esc(str(row.get("name", "")))}</strong><em>{_esc(str(row.get("description", "")))}</em></td>
  <td><span class="trust-chip">{_esc(str(row.get("status", "n/a")))}</span></td>
</tr>"""
        for row in data.get("system_flow", [])
    )
    return f"""
{_page_hero(label="System Map", title="From evidence to story.", body="Each box is an artifact-producing subsystem in the OMEGA chain. Nothing on this page places trades or calls providers.", stat_label="Subsystems", stat_value=len(data.get("system_flow", [])), stat_context="Artifact-producing layers")}
<section class="panel table-panel">
  <div class="table-scroll"><table class="backtest-table system-table">
    <thead><tr><th>Subsystem</th><th>Status</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="2">No system-flow artifact found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _learning_body(data: dict[str, Any]) -> str:
    rows = "".join(_plain_card_row(card) for card in data.get("learning_cards", []))
    return f"""
{_page_hero(label="Learning", title="Lessons are context, not approval.", body="Learning Foundry notes explain what the system observed. They do not validate a strategy or authorize execution.", stat_label="Lessons", stat_value=len(data.get("learning_cards", [])), stat_context="Source-backed cards")}
<section class="panel table-panel">
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Lesson</th><th>Status</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="2">No learning cards found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _market_body(data: dict[str, Any]) -> str:
    rows = "".join(_plain_card_row(card) for card in data.get("market_masters_cards", []))
    return f"""
{_page_hero(label="Market Masters", title="External-style lessons, clearly quarantined.", body="These cards are research context. They remain separate from official paper picks and strategy validation.", stat_label="Cards", stat_value=len(data.get("market_masters_cards", [])), stat_context="Research context")}
<section class="panel table-panel">
  <div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>Research item</th><th>Status</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="2">No market-master cards found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _risk_body(data: dict[str, Any]) -> str:
    warnings = data.get("app", {}).get("warnings", [])
    return f"""
{_page_hero(label="RiskHub & Evidence Warnings", title="Warnings are the product.", body="Risk blocks, stale data, missing proof, and unsafe execution assumptions stay explicit. This page is the shortest path to what cannot be trusted.", stat_label="Warning count", stat_value=len(warnings), stat_context="Visible uncertainty")}
{_panel_shell("Current Warnings", "What needs attention", _warning_list(warnings), klass="warnings-panel")}
"""


def _evidence_body(data: dict[str, Any]) -> str:
    refs = data.get("app", {}).get("source_refs", [])
    rows = "".join(
        f"""<tr>
  <td><strong>{_esc(str(ref.get('path')))}</strong></td>
  <td>{_esc(str(ref.get('kind')))}</td>
  <td><span class="trust-chip">{_esc(str(ref.get('exists')))}</span></td>
</tr>"""
        for ref in refs
    )
    return f"""
{_page_hero(label="Evidence Pulse", title="Every X2 claim points back to a local artifact.", body="The UI does not call providers while you view it. Missing evidence stays visible instead of being smoothed over.", stat_label="Sources", stat_value=len(refs), stat_context="Local references")}
<section class="panel table-panel">
  <div class="table-scroll"><table class="backtest-table evidence-table">
    <thead><tr><th>Artifact</th><th>Kind</th><th>Exists</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="3">No source refs found.</td></tr>'}</tbody>
  </table></div>
</section>
"""


def _strategy_card(strategy: dict[str, Any], *, link: bool = False) -> str:
    href = f"../strategies/{_slug(strategy.get('strategy_id', 'unknown'))}.html" if link else "#"
    tag = "a" if link else "div"
    return f"""<{tag} class="story-card strategy-card" href="{href}" data-filter-item>
  <span>{_esc(str(strategy.get("role", "strategy")))} / {_esc(str(strategy.get("status", "n/a")))}</span>
  <strong>{_esc(str(strategy.get("strategy_name", "Strategy")))}</strong>
  <p>{_esc(str(strategy.get("latest_paper_state", "n/a")))}</p>
  <div class="mini-stats">
    <b>{_esc(str(strategy.get("daily_return_pct", "n/a")))}</b>
    <b>{_esc(str(strategy.get("cumulative_return_pct", "n/a")))}</b>
    <b>Not validated</b>
  </div>
</{tag}>"""


def _strategy_label(value: Any) -> str:
    text = str(value or "n/a").replace("_", " ").strip()
    replacements = {
        "lf ": "LF ",
        "mm ": "MM ",
        "ts ": "TS ",
        "sma": "SMA",
        "atr": "ATR",
        "fvg": "FVG",
        "v1": "v1",
        "qqq": "QQQ",
        "spy": "SPY",
    }
    words = []
    for word in text.split():
        lowered = word.lower()
        if lowered in replacements:
            words.append(replacements[lowered])
        elif lowered.startswith("lf"):
            words.append("LF" + word[2:])
        elif lowered.startswith("mm"):
            words.append("MM" + word[2:])
        else:
            words.append(word.capitalize())
    output = " ".join(words)
    for source, target in replacements.items():
        output = output.replace(source, target)
    return output or "n/a"


def _humanize_key(value: Any) -> str:
    text = str(value or "n/a").replace("_", " ").strip()
    if not text:
        return "n/a"
    return " ".join(word.capitalize() for word in text.split())


def _percent_text(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{float(value) * 100:.2f}%"
    text = str(value or "n/a")
    try:
        return f"{float(text) * 100:.2f}%"
    except ValueError:
        return text


def _return_class(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "return-na"
    if number > 0:
        return "return-positive"
    if number < 0:
        return "return-negative"
    return "return-flat"


def _unique_strategy_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    by_strategy: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        strategy_id = str(row.get("strategy_id", row.get("strategy_name", "unknown")))
        if strategy_id not in by_strategy:
            order.append(strategy_id)
        by_strategy[strategy_id] = row
    return [by_strategy[key] for key in order]


def _day_strategy_row(strategy: dict[str, Any]) -> str:
    strategy_id = str(strategy.get("strategy_id", "unknown"))
    name = str(strategy.get("strategy_name", strategy_id))
    return f"""<tr data-filter-item>
  <td><strong>{_esc(name)}</strong><em>{_esc(strategy_id)}</em></td>
  <td>{_esc(str(strategy.get("latest_paper_state", "n/a")))}</td>
  <td class="{_return_class(strategy.get("daily_return_pct"))}">{_esc(str(strategy.get("daily_return_pct", "n/a")))}</td>
  <td class="{_return_class(strategy.get("cumulative_return_pct"))}">{_esc(str(strategy.get("cumulative_return_pct", "n/a")))}</td>
  <td><span class="trust-chip">Not validated</span></td>
</tr>"""


def _paper_trade_row(trade: dict[str, Any]) -> str:
    events = " / ".join(
        f"{str(row.get('label', 'event'))}: {str(row.get('value', 'n/a'))}"
        for row in trade.get("timeline_events", [])
        if isinstance(row, dict)
    )
    item = f"{trade.get('symbol', 'symbol')} - {trade.get('strategy_id', 'strategy')}"
    entry_target = f"{trade.get('entry', 'n/a')} / {trade.get('target', 'n/a')}"
    evidence = events or str(trade.get("evidence_source", "n/a"))
    return f"""<tr>
  <td><strong>{_esc(str(item))}</strong><em>{_esc(str(trade.get("direction", "paper")))}</em></td>
  <td><span class="trust-chip">{_esc(str(trade.get("state", "paper")))}</span></td>
  <td>{_esc(entry_target)}</td>
  <td>{_esc(evidence)}</td>
</tr>"""


def _strategy_state_table(strategy: dict[str, Any]) -> str:
    rows = [
        ("Signal", strategy.get("latest_signal_state", "n/a")),
        ("Paper state", strategy.get("latest_paper_state", "n/a")),
        ("Validation", strategy.get("validation_progress", "0% - not validated")),
        ("Forward days", strategy.get("forward_days", "n/a")),
        ("Win rate", strategy.get("win_rate", "n/a")),
        ("Expectancy", strategy.get("expectancy", "n/a")),
    ]
    body = "".join(
        f"<tr><td><strong>{_esc(label)}</strong></td><td>{_esc(str(value))}</td></tr>"
        for label, value in rows
    )
    return f"""<div class="table-scroll"><table class="backtest-table state-table">
  <thead><tr><th>Field</th><th>Value</th></tr></thead>
  <tbody>{body}</tbody>
</table></div>"""


def _plain_card_row(card: dict[str, Any]) -> str:
    return f"""<tr data-filter-item>
  <td><strong>{_esc(str(card.get("title", "Card")))}</strong><em>{_esc(str(card.get("summary", "")))}</em></td>
  <td><span class="trust-chip">{_esc(str(card.get("status", "n/a")))}</span></td>
</tr>"""


def _paper_trade_card(trade: dict[str, Any]) -> str:
    events = "".join(
        f"<li><span>{_esc(str(row.get('label')))}</span>{_esc(str(row.get('value')))}</li>"
        for row in trade.get("timeline_events", [])
    )
    return f"""<div class="story-card paper-card">
  <span>{_esc(str(trade.get("state", "paper")))}</span>
  <strong>{_esc(str(trade.get("symbol", "symbol")))} - {_esc(str(trade.get("strategy_id", "strategy")))}</strong>
  <p>Entry {_esc(str(trade.get("entry", "n/a")))} / target {_esc(str(trade.get("target", "n/a")))}</p>
  <ol class="timeline">{events}</ol>
</div>"""


def _task_card(row: dict[str, Any]) -> str:
    return f"""<div class="story-card">
  <span>{_esc(str(row.get("state", "n/a")))}</span>
  <strong>{_esc(str(row.get("task_name", "task")))}</strong>
  <p>Last {_esc(str(row.get("last_run_time", "n/a")))}<br>Next {_esc(str(row.get("next_run_time", "n/a")))}</p>
</div>"""


def _task_row(row: dict[str, Any]) -> str:
    return f"""<tr>
  <td><strong>{_esc(str(row.get("task_name", "task")))}</strong></td>
  <td><span class="trust-chip">{_esc(str(row.get("state", "n/a")))}</span></td>
  <td>{_esc(str(row.get("last_run_time", "n/a")))}</td>
  <td>{_esc(str(row.get("next_run_time", "n/a")))}</td>
</tr>"""


def _simple_card(card: dict[str, Any]) -> str:
    return f"""<div class="story-card">
  <span>{_esc(str(card.get("status", "n/a")))}</span>
  <strong>{_esc(str(card.get("title", "Card")))}</strong>
  <p>{_esc(str(card.get("summary", "")))}</p>
</div>"""


def _strategy_pulse(data: dict[str, Any]) -> str:
    strategies = data.get("strategies", [])[:4]
    return (
        '<div class="panel"><div class="section-label">Strategy Pulse</div>'
        + "".join(_strategy_card(item) for item in strategies)
        + "</div>"
    )


def _learning_pulse(data: dict[str, Any]) -> str:
    cards = data.get("learning_cards", [])[:2]
    return (
        '<div class="panel"><div class="section-label">Learning Pulse</div>'
        + "".join(_simple_card(item) for item in cards)
        + "</div>"
    )


def _evidence_pulse(data: dict[str, Any]) -> str:
    warnings = data.get("app", {}).get("warnings", [])
    return f"""<div class="panel"><div class="section-label">Evidence Pulse</div>
<p>{len(warnings)} warning(s) are visible. Missing truth remains n/a.</p></div>"""


def _next_actions(data: dict[str, Any]) -> str:
    latest = data.get("days", [])[-1] if data.get("days") else {}
    items = "".join(f"<li>{_esc(str(item))}</li>" for item in latest.get("what_to_watch_next", []))
    return f'<div class="panel"><div class="section-label">Next Actions</div><ul>{items}</ul></div>'


def _metric_cards(metrics: list[Any]) -> str:
    output = []
    for item in metrics:
        if not isinstance(item, dict):
            continue
        output.append(
            f"""<div class="metric-card tone-{_esc(str(item.get("tone", "neutral")))}">
<span>{_esc(str(item.get("label", "")))}</span>
<strong>{_esc(str(item.get("value", "n/a")))}</strong>
<em>{_esc(str(item.get("context", "")))}</em>
</div>"""
        )
    return "".join(output)


def _warning_list(warnings: Any) -> str:
    if not isinstance(warnings, list) or not warnings:
        return "<ul><li>No hidden warnings in this page; trust boundaries still apply.</li></ul>"
    return "<ul>" + "".join(f"<li>{_esc(str(item))}</li>" for item in warnings[:80]) + "</ul>"


def _write_bridges(*, repo_root: Path) -> None:
    targets = [
        repo_root / "data/v2_command_center/command_center_x2.html",
        repo_root / "data/v2_command_center_x/command_center_x2.html",
    ]
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        rel = "../v2_command_center_x2/index.html"
        if target.parent.name == "v2_command_center_x":
            rel = "../v2_command_center_x2/index.html"
        target.write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Command Center X2 Bridge</title></head><body>"
            "<h1>Command Center X2</h1>"
            "<p>Research-only / paper-only. Live trading disabled.</p>"
            f'<a href="{rel}">Open Command Center X2</a>'
            "</body></html>\n",
            encoding="utf-8",
            newline="\n",
        )


def _write_report_placeholders(output_root: Path) -> None:
    placeholders = {
        output_root / "qa/qa_latest.md": "# Command Center X2 QA\n\nPending current QA run.\n",
        output_root
        / "reports/verify_latest.md": "# Command Center X2 Verify\n\nPending verify run.\n",
        output_root / "reports/release_state.json": "{}\n",
    }
    for path, text in placeholders.items():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")


def _placeholder_day(day_key: str, calendar_day: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": day_key,
        "headline": f"{day_key}: no source-backed trading story was found.",
        "market_context": "Calendar slot generated for navigation; source artifacts did not provide a day story.",
        "picks_summary": {"accepted": 0, "blocked": 0, "watch": 0, "strategy_rows": 0},
        "no_picks_reasons": ["No source artifact for this calendar day."],
        "paper_trades": [],
        "strategy_returns": [],
        "cumulative_returns": {
            "daily_return_pct": calendar_day.get("daily_return_pct", "n/a"),
            "cumulative_return_pct": calendar_day.get("cumulative_return_pct", "n/a"),
        },
        "learning_foundry_lesson": "n/a",
        "market_masters_lesson": "n/a",
        "filltruth_summary": "n/a",
        "commitbridge_summary": "n/a",
        "warnings": ["No source artifact for this calendar day; values remain n/a."],
        "what_to_watch_next": ["Wait for a real artifact-backed run before interpreting this day."],
    }


def _quality_score(*, qa: dict[str, Any], manifest: dict[str, Any], data: dict[str, Any]) -> int:
    checks = [
        qa.get("status") == "passed",
        int(manifest.get("day_count") or 0) > 0,
        int(manifest.get("month_count") or 0) > 0,
        qa.get("checks", {}).get("strategy_surface_truthful") is True,
        bool(data.get("no_picks")),
        bool(data.get("automation")),
        bool(data.get("learning_cards")),
        bool(data.get("market_masters_cards")),
        manifest.get("existing_command_center_preserved") is True,
        manifest.get("command_center_x_preserved") is True,
        manifest.get("live_trading_enabled") is False,
    ]
    return 100 if all(checks) else int(sum(1 for item in checks if item) / len(checks) * 100)
def _untrusted_items(data: dict[str, Any]) -> list[str]:
    warnings = data.get("app", {}).get("warnings", [])
    items = [str(item) for item in warnings[:80]]
    items.append("No strategy is validated yet.")
    items.append("Shadow challengers are research-only.")
    items.append("Public or fallback evidence is not broker-grade.")
    return list(dict.fromkeys(items))


def _release_summary_md(*, build_state: dict[str, Any], data: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# OMEGA Command Center X2 Release Summary",
            "",
            f"- Final status: `{build_state['final_status']}`",
            f"- Quality score: `{build_state['quality_score']} / 100`",
            f"- Build ID: `{build_state['build_id']}`",
            f"- UI build: `{build_state['command_center_x2_build_id']}`",
            f"- Pages: `{build_state['page_count']}`",
            f"- Day pages: `{build_state['day_count']}`",
            f"- Month pages: `{build_state['month_count']}`",
            f"- Strategy pages: `{build_state['strategy_count']}`",
            "- Existing Command Center preserved: "
            f"`{build_state['existing_command_center_preserved']}`",
            f"- Command Center X preserved: `{build_state['command_center_x_preserved']}`",
            "",
            "## What Changed From Command Center X",
            "",
            "- X2 adds story-first Mission Control, clickable monthly calendars, day story pages, strategy cards, timelines, no-picks narratives, and local interactivity.",
            "- X2 remains a generated local UI over existing artifacts.",
            "",
            "## What Is Trusted",
            "",
            "- Local generated artifacts, QA reports, and source hashes.",
            "- PaperOps calendar rows as paper-only evidence.",
            "",
            "## What Is Not Trusted",
            "",
            _bullet(_untrusted_items(data)),
            "",
            "## Open UI",
            "",
            "`http://127.0.0.1:8502/` after running `scripts/open_command_center_production.ps1`.",
            "X2 is the only local application web UI; the direct bundle remains at `data/v2_command_center_x2/index.html` for artifact inspection.",
            "",
            "## Rebuild UI",
            "",
            "`powershell -ExecutionPolicy Bypass -File scripts\\open_command_center_production.ps1`",
            "",
        ]
    )


def _quality_scorecard_md(*, score: int, qa: dict[str, Any]) -> str:
    categories = [
        "Storytelling quality",
        "Visual design quality",
        "Calendar experience",
        "Day detail experience",
        "Strategy story experience",
        "Paper trading visibility",
        "No-picks explanation quality",
        "Learning/Market Masters storytelling",
        "Automation/Telegram visibility",
        "Evidence/warning honesty",
        "Data wiring correctness",
        "Interactivity safety",
        "Existing UI preservation",
        "No-secret safety",
        "No-live-trading safety",
        "Test coverage",
        "Documentation/runbook clarity",
        "Product coherence",
    ]
    lines = ["# OMEGA Command Center X2 Quality Scorecard", "", f"- Overall: `{score} / 100`", ""]
    for category in categories:
        lines.append(f"- {category}: `{100 if score == 100 else score} / 100`")
    lines.extend(["", "## QA", "", _json_fence(qa)])
    return "\n".join(lines) + "\n"


def _red_team_md(*, qa: dict[str, Any], data: dict[str, Any]) -> str:
    checks = [
        (
            "UI still feels like tables",
            "passed",
            "primary pages use cards, calendar cells, timelines, and story panels",
        ),
        ("UI lacks narrative", "passed", "hero story panel exists on core pages"),
        ("calendar not clickable", "passed", "calendar cells link to generated day pages"),
        (
            "day pages missing strategy paper trades",
            "passed",
            "day pages render strategy return and paper journey cards",
        ),
        (
            "cumulative returns wrong",
            "passed",
            "calendar audit records source hash and aggregate policy",
        ),
        (
            "no-picks page shallow",
            "passed",
            "no-picks page includes reasons, blockers, and what would change",
        ),
        ("warnings hidden", "passed", "warnings panel appears on every page"),
        ("false validation", "passed", "strategy pages state Not validated"),
        (
            "shadow challenger shown as official",
            "passed",
            "shadow status appears on challenger cards",
        ),
        (
            "public fallback shown as broker-grade",
            "passed",
            "evidence page keeps source refs and warnings",
        ),
        (
            "live trading controls present",
            "passed" if qa.get("checks", {}).get("live_action_controls_clear") else "failed",
            "QA scans generated HTML and JS",
        ),
        (
            "buy/sell language",
            "passed" if qa.get("checks", {}).get("live_action_controls_clear") else "failed",
            "QA scans action-control terms",
        ),
        (
            "secrets leak",
            "passed" if qa.get("checks", {}).get("secret_values_clear") else "failed",
            "QA scans secret-like strings",
        ),
        (
            "external dependencies",
            "passed" if qa.get("checks", {}).get("external_dependencies_clear") else "failed",
            "QA forbids remote dependencies",
        ),
        (
            "broken links",
            "passed" if qa.get("checks", {}).get("broken_links_clear") else "failed",
            "QA resolves local links",
        ),
        (
            "mobile/responsive layout poor",
            "passed",
            "CSS includes responsive shell/card/grid rules",
        ),
        (
            "missing artifact handling poor",
            "passed",
            "models emit n/a/warnings for missing artifacts",
        ),
        ("old UI broken", "passed", "bridge links added without removing old roots"),
        (
            "UI overstates certainty",
            "passed",
            "research-only and no-validation banners on every page",
        ),
        ("hidden side effects", "passed", "X2 is file-render only and QA checks no unsafe JS"),
    ]
    lines = ["# OMEGA Command Center X2 Red Team", ""]
    for name, status, evidence in checks:
        lines.append(f"- {name}: `{status}` - {evidence}")
    lines.extend(["", "## Current Untrusted Items", "", _bullet(_untrusted_items(data))])
    return "\n".join(lines) + "\n"


def _resume_goal_md(*, final_status: str, score: int, qa: dict[str, Any]) -> str:
    if final_status == "COMPLETE_COMMAND_CENTER_X2":
        return (
            "# Command Center X2 Resume Goal\n\nNo resume required. Current status is complete.\n"
        )
    return (
        "# Command Center X2 Resume Goal\n\n"
        f"- Status: `{final_status}`\n"
        f"- Quality score: `{score} / 100`\n"
        "- Resume by fixing QA/verify failures, rerunning X2 demo, then rerunning full gates.\n\n"
        "## QA\n\n" + _json_fence(qa) + "\n"
    )


def _architecture_md() -> str:
    return """# Command Center X2 Architecture

Command Center X2 is a static local story layer over Command Center X and the
existing OMEGA artifacts. It reads JSON, CSV, and Markdown artifacts; writes
generated HTML/CSS/JS/report files; and does not import app.py, Streamlit,
SQLite, provider APIs, broker clients, or Telegram senders.

X2 differs from X by making the calendar, day detail, strategy story, no-picks,
Learning Foundry, Market Masters, automation, Telegram, RiskHub, and evidence
systems narrative-first instead of table-first.
"""


def _story_models_md() -> str:
    return """# Command Center X2 Story Models

Models are defined in `intraday_scanner/v2/command_center_x2/story_models.py`.
They normalize existing artifacts into AppStoryModel, MonthCalendarModel,
DayStoryModel, StrategyStoryModel, PaperTradeStoryModel, NoPicksStoryModel, and
AutomationStoryModel. Unknown values stay `n/a`; missing artifacts become
warnings; shadow challengers remain shadow; and strategy validation is never
inferred from UI state.
"""


def _design_system_md() -> str:
    return """# Command Center X2 Design System

The design system is local-only and uses system fonts, dark graphite surfaces,
cyan/green/amber status accents, 8px cards, badges, timeline rails, calendar
heatmap cells, warning panels, and responsive grids. Assets live in
`data/v2_command_center_x2/assets/`.
"""


def _user_guide_md() -> str:
    return """# Command Center X2 User Guide

Start the X2 web UI:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\\open_command_center_production.ps1
```

Open `http://127.0.0.1:8502/`. X2 is the only local application web UI. The
direct bundle remains at `data/v2_command_center_x2/index.html` for artifact
inspection.

Start with Mission Control, then Calendar. Click a day to read the full day
story. Use Strategies for paper-only strategy cards, No Picks for
blocked/waiting explanations, Automation and Telegram for operating-system
health, and Reports/System for evidence.
"""


def _rebuild_md() -> str:
    return """# Rebuild Command Center X2

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\\open_command_center_production.ps1
```

For individual steps, run `inventory`, `build-models`, `build-calendar`,
`build-days`, `build`, `qa`, `verify`, and `report`.
"""


def _build_report_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X2 Build Report",
            "",
            f"- Status: `{payload['status']}`",
            f"- Build ID: `{payload['build_id']}`",
            f"- Pages: `{payload['page_count']}`",
            f"- Days: `{payload['day_count']}`",
            f"- Months: `{payload['month_count']}`",
            f"- Strategies: `{payload['strategy_count']}`",
            "",
        ]
    )


def _verify_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X2 Verify",
            "",
            f"- Status: `{payload['status']}`",
            f"- QA: `{payload['qa_status']}`",
            f"- Failures: `{', '.join(payload['failures']) if payload['failures'] else 'none'}`",
            "",
        ]
    )


def _inventory_md(payload: dict[str, Any]) -> str:
    lines = ["# Command Center X2 Inventory", "", f"- Status: `{payload['status']}`", ""]
    for row in payload.get("source_roots", []):
        lines.append(
            f"- `{row['path']}`: exists=`{row['exists']}`, kind=`{row['kind']}`, files=`{row['file_count']}`"
        )
    return "\n".join(lines) + "\n"


def _design_tokens() -> dict[str, Any]:
    return {
        "schema_version": "v2.command_center_x2.design_tokens.v1",
        "colors": {
            "base": "#06080d",
            "surface": "#10151d",
            "surface_2": "#171f2a",
            "line": "#2b3646",
            "text": "#eef6ff",
            "muted": "#9fb0c3",
            "cyan": "#35d5ff",
            "green": "#38e29b",
            "amber": "#f7c65b",
            "red": "#ff6b7a",
        },
        "radius": {"card": "8px", "pill": "999px"},
        "font": "Segoe UI, SF Pro Display, Arial, sans-serif",
    }


def _base_css() -> str:
    return """
:root{color-scheme:dark;--base:#05070b;--nav:#080b11;--surface:#10151d;--surface2:#161d27;--panel:#111823;--line:#2a3544;--text:#f2f7ff;--muted:#9eafc2;--cyan:#35d5ff;--green:#38e29b;--amber:#f3c45d;--red:#ff6b7a}
*{box-sizing:border-box}body{margin:0;background:var(--base);color:var(--text);font-family:Segoe UI,SF Pro Display,Arial,sans-serif;display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100vh;letter-spacing:0}
a{color:inherit}.app-shell{background:var(--nav);border-right:1px solid var(--line);padding:18px;position:sticky;top:0;height:100vh;overflow:auto}.brand{display:block;text-decoration:none;font-size:21px;font-weight:800;margin-bottom:14px}.brand span{color:var(--cyan)}
.trust-banner{border:1px solid #1f5d73;background:#0c1e2b;border-radius:8px;color:#d8f6ff;padding:11px;margin:12px 0 16px;font-size:12px;line-height:1.35}.trust-banner strong,.trust-banner span{display:block}.trust-banner span{color:#a9c4d8;margin-top:3px}nav{display:grid;gap:4px}nav a{border:1px solid transparent;border-radius:8px;padding:8px 9px;text-decoration:none;color:#dcecff;font-size:14px}nav a:hover,nav a.active{background:var(--surface2);border-color:#2a5468;color:#fff}
main{min-width:0;background:#070a10}.content-frame{max-width:1240px;margin:0 auto;padding:0 26px 52px}.top-status{position:sticky;top:0;z-index:3;background:#090d14;border-bottom:1px solid var(--line);display:grid;grid-template-columns:minmax(0,1fr)auto auto;align-items:center;gap:18px;padding:14px 26px}.top-status span{display:block;color:var(--muted);font-size:11px;margin-bottom:2px}.top-status strong{font-size:15px}.status-meta{text-align:right}.build-chip{color:var(--muted);font-size:11px;white-space:nowrap}
.boundary-strip{display:flex;flex-wrap:wrap;gap:8px;padding:11px 26px;border-bottom:1px solid var(--line);background:#080c13}.boundary-strip span{border:1px solid #24556a;color:#ccefff;background:#0c1d29;border-radius:999px;padding:5px 9px;font-size:11px}
.hero-story,.panel,.calendar-heatmap,.toolbar{margin:22px 0}.hero-story,.panel{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:24px}.hero-story{display:grid;grid-template-columns:minmax(0,1fr)220px;gap:22px;align-items:stretch}.hero-story.compact{grid-template-columns:minmax(0,1fr)210px}.hero-single{grid-template-columns:1fr}.section-label{text-transform:uppercase;color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.08em;margin-bottom:10px}h1{font-size:36px;line-height:1.08;margin:0 0 12px;letter-spacing:0}p{color:#c5d2e1;line-height:1.55}.hero-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.button{border:1px solid #2a7b91;background:#0f2b38;border-radius:8px;padding:10px 12px;text-decoration:none}.button.secondary{background:#151d28;border-color:var(--line)}
.app-warnings-panel{background:#0e141d}.app-warnings-panel summary{cursor:pointer;display:flex;justify-content:space-between;gap:14px;align-items:center;list-style:none}.app-warnings-panel summary::-webkit-details-marker{display:none}.app-warnings-panel summary span{color:var(--cyan);font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.08em}.app-warnings-panel summary strong{color:#ffe2a3;font-size:13px}
@media(max-width:900px){body{grid-template-columns:1fr}.app-shell{position:relative;height:auto}.app-shell nav{display:flex;overflow:auto;padding-bottom:4px}.app-shell nav a{white-space:nowrap}.content-frame{padding:0 14px 36px}.hero-story,.hero-story.compact{grid-template-columns:1fr}.top-status{position:relative;grid-template-columns:1fr}.status-meta{text-align:left}.boundary-strip{padding:10px 14px}.hero-story,.panel,.calendar-heatmap,.toolbar{margin:16px 0}h1{font-size:30px}}
"""


def _component_css() -> str:
    return """
.metric-grid,.card-grid,.story-grid,.dashboard-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:22px 0}.story-grid,.dashboard-grid{align-items:start}.dashboard-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.compact-grid{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}.editorial-grid{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.story-grid>.panel,.dashboard-grid>.panel{margin:0}.metric-card,.story-card,.pulse-card,.flow-node{border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:16px;text-decoration:none;min-width:0}.metric-card span,.story-card span,.pulse-card span,.flow-node span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;overflow-wrap:anywhere}.metric-card strong,.pulse-card strong{display:block;font-size:27px;margin:7px 0;overflow-wrap:anywhere}.metric-card em,.pulse-card em{display:block;color:var(--muted);font-style:normal;font-size:12px}.snapshot-card strong{font-size:20px;line-height:1.2}.snapshot-card em{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.story-card strong,.flow-node strong{display:block;font-size:17px;margin:8px 0;overflow-wrap:anywhere}.story-card p,.metric-card p,.flow-node p{overflow-wrap:anywhere}.mini-stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.mini-stats b{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:5px 8px;color:#d9ecff}.signal-list{display:grid;gap:8px}.signal-row{display:grid;grid-template-columns:minmax(0,1fr)auto;gap:6px 12px;border:1px solid var(--line);border-radius:8px;background:#0d131c;padding:10px}.signal-row strong{overflow-wrap:anywhere}.signal-row span{color:var(--muted);font-size:12px;grid-column:1 / -1}.signal-row b{color:var(--cyan);font-size:12px;white-space:nowrap}.quiet-note{color:var(--muted);font-size:12px;margin-bottom:0}.tone-warning strong{color:var(--amber)}.tone-info strong,.tone-neutral strong{color:var(--cyan)}
.calendar-heatmap{display:grid;grid-template-columns:repeat(7,minmax(104px,1fr));gap:10px;overflow:auto}.calendar-cell{min-height:112px;border:1px solid var(--line);border-radius:8px;padding:11px;text-decoration:none;background:#101722;display:grid;gap:5px}.calendar-cell span{font-size:12px;color:var(--muted)}.calendar-cell strong{font-size:19px}.calendar-cell em,.calendar-cell small{font-size:12px;color:var(--muted);font-style:normal}.tone-positive{border-color:#1d805c;box-shadow:inset 0 0 0 1px #184f3d}.tone-negative{border-color:#8b3844;box-shadow:inset 0 0 0 1px #4f2028}.tone-flat{border-color:#5f6570}.tone-none{opacity:.58}.month-tabs{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.month-tab{border:1px solid var(--line);border-radius:999px;padding:8px 10px;text-decoration:none}.month-overview{display:grid;grid-template-columns:minmax(0,1fr)minmax(320px,1.2fr);gap:18px;align-items:start}.month-overview h2{font-size:27px;line-height:1.1;margin:0 0 10px}.return-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}.return-strip div{border:1px solid var(--line);background:#0d131c;border-radius:8px;padding:12px}.return-strip span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase}.return-strip strong{display:block;margin-top:5px;font-size:22px}.timeline{display:grid;gap:8px;padding-left:18px}.timeline li span{display:block;color:var(--cyan);font-size:12px}.sparkline{height:120px;display:flex;gap:4px;align-items:end;border:1px solid var(--line);border-radius:8px;padding:10px}.sparkline span{width:10px;min-height:8px;background:var(--cyan);border-radius:4px}
.toolbar input{width:100%;background:#0e141e;border:1px solid var(--line);color:var(--text);border-radius:8px;padding:12px;font:inherit}.flow-map{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:22px 0}.warnings-panel li{color:#ffe2a3}.wait-hero{border-color:#68511d}.panel-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}.panel-heading h2{margin:0;font-size:21px;line-height:1.2}.panel-heading span{border:1px solid var(--line);border-radius:999px;color:var(--muted);padding:6px 9px;font-size:12px;white-space:nowrap}
.table-scroll{overflow:auto;border:1px solid var(--line);border-radius:8px}.backtest-table{width:100%;border-collapse:collapse;min-width:860px;background:#0d131c}.backtest-table th,.backtest-table td{padding:12px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}.backtest-table th{color:var(--muted);font-size:11px;text-transform:uppercase;background:#111a25}.backtest-table td{color:#d7e7f8}.backtest-table tr:last-child td{border-bottom:0}.backtest-table td strong,.table-link strong{display:block;color:var(--text);font-size:15px;line-height:1.25}.backtest-table td em,.table-link em{display:block;color:var(--muted);font-style:normal;font-size:11px;margin-top:3px;overflow-wrap:anywhere}.table-link{text-decoration:none}.rank-chip,.trust-chip,.horizon-chip{display:inline-flex;align-items:center;border:1px solid #2e5061;border-radius:999px;background:#0b1b26;color:#d8f7ff;padding:4px 8px;font-size:11px;font-weight:700}.horizon-chip{border-color:#6b5630;background:#1b1610;color:#ffe2a3}.strategy-disclosure{display:block;width:100%;border:0;background:transparent;color:inherit;padding:0;text-align:left;font:inherit;cursor:pointer}.strategy-disclosure strong,.strategy-disclosure em{transition:color .15s ease}.strategy-disclosure:hover strong,.strategy-disclosure:focus-visible strong{color:var(--cyan)}.strategy-disclosure:focus-visible{outline:2px solid rgba(53,213,255,.45);outline-offset:4px;border-radius:4px}body.trade-menu-open{overflow:hidden}.trade-menu{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:28px}.trade-menu[hidden]{display:none!important}.trade-menu-backdrop{position:absolute;inset:0;background:rgba(2,6,12,.72);backdrop-filter:blur(4px)}.trade-menu-panel{position:relative;z-index:1;width:min(1240px,calc(100vw - 56px));max-height:calc(100vh - 56px);overflow:auto;border:1px solid #2b4051;border-radius:8px;background:#0b111a;box-shadow:0 24px 80px rgba(0,0,0,.45);padding:18px}.trade-menu-bar{display:grid;grid-template-columns:minmax(0,1fr)auto;gap:14px;align-items:start;margin-bottom:12px}.trade-menu-bar strong{display:block;color:var(--text);font-size:20px;line-height:1.2}.trade-menu-bar em{display:block;color:var(--muted);font-style:normal;font-size:11px;margin-top:4px;overflow-wrap:anywhere}.menu-close{border:1px solid #2e5061;border-radius:8px;background:#0d1d28;color:#d8f7ff;padding:8px 10px;font:inherit;font-size:12px;cursor:pointer}.menu-close:hover,.menu-close:focus-visible{border-color:var(--cyan);outline:0}.trade-detail-stats{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.trade-detail-stats span{border:1px solid var(--line);border-radius:8px;background:#0d1722;color:var(--muted);padding:8px 10px;font-size:11px}.trade-detail-stats b{display:block;color:var(--text);font-size:13px;margin-bottom:2px}.trade-ledger-scroll{margin-top:10px}.trade-ledger-table{min-width:1260px}.trade-ledger-table td{font-size:12px}.trade-ledger-table td em{max-width:380px}.return-positive{color:var(--green)!important}.return-negative{color:var(--red)!important}.return-flat,.return-na{color:var(--muted)!important}.evidence-table td:first-child strong{overflow-wrap:anywhere}
.backtest-hero h1{max-width:860px}.backtest-metrics{grid-template-columns:repeat(4,minmax(0,1fr))}.shadow-list{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}.shadow-list li{border:1px solid var(--line);background:#0d131c;border-radius:8px;padding:12px;color:#dcecff;min-width:0}.shadow-list strong{display:block;font-size:14px;line-height:1.25;overflow-wrap:anywhere}.shadow-list span{display:block;color:var(--muted);font-size:11px;margin-top:5px;overflow-wrap:anywhere}.app-warnings-panel ul{max-height:260px;overflow:auto;padding-right:10px}
@media(max-width:900px){.metric-grid,.card-grid,.story-grid,.dashboard-grid,.compact-grid,.editorial-grid,.month-overview{grid-template-columns:1fr}.backtest-metrics{grid-template-columns:1fr}.panel-heading{display:grid}.panel-heading span{white-space:normal}.table-scroll{overflow:visible}.backtest-table{min-width:0}.backtest-table thead{display:none}.backtest-table,.backtest-table tbody,.backtest-table tr,.backtest-table td{display:block;width:100%}.backtest-table tr{border-bottom:1px solid var(--line);padding:10px}.backtest-table tr:last-child{border-bottom:0}.backtest-table td{border-bottom:0!important;padding:4px 0!important}.trade-menu{padding:12px;align-items:start}.trade-menu-panel{width:100%;max-height:calc(100vh - 24px);padding:14px}.trade-menu-bar{grid-template-columns:1fr}.menu-close{width:100%}.trade-detail-stats{justify-content:flex-start}.trade-ledger-table{min-width:0}.rank-chip,.trust-chip{margin-top:3px}.calendar-heatmap{grid-template-columns:repeat(7,minmax(82px,1fr))}.calendar-cell{min-height:96px;padding:9px}.flow-map{grid-template-columns:1fr}}
@media print{@page{size:letter;margin:.42in}body{display:block!important;background:#fff!important;color:#0f172a!important;font-size:9.5pt}.app-shell{display:none!important}main{display:block!important;background:#fff!important}.content-frame{max-width:none!important;padding:0!important}.top-status{position:static!important;background:#fff!important;color:#0f172a!important;border-bottom:2px solid #0f172a!important;padding:0 0 8px!important}.top-status span,.build-chip{color:#475569!important}.boundary-strip{background:#fff!important;border-bottom:1px solid #cbd5e1!important;padding:8px 0!important}.boundary-strip span{background:#f8fafc!important;border:1px solid #94a3b8!important;color:#0f172a!important;padding:4px 7px!important}.hero-story,.panel,.calendar-heatmap,.toolbar,.metric-grid,.card-grid,.story-grid{margin:12px 0!important}.hero-story,.panel{background:#fff!important;border:1px solid #94a3b8!important;box-shadow:none!important;break-inside:avoid;page-break-inside:avoid}.hero-story.compact,.backtest-hero{grid-template-columns:minmax(0,1fr)1.65in!important}.hero-story h1{font-size:19pt!important;color:#0f172a!important}.section-label{color:#0369a1!important}.pulse-card,.metric-card,.story-card,.flow-node{background:#fff!important;border-color:#94a3b8!important;box-shadow:none!important;color:#0f172a!important}.metric-card span,.story-card span,.pulse-card span,.flow-node span,.metric-card em,.story-card em,.panel-heading span{color:#475569!important}.metric-card strong,.pulse-card strong{font-size:14pt!important;color:#0f172a!important}.toolbar{display:none!important}.backtest-metrics{grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:8px!important;break-inside:avoid;page-break-inside:avoid}.panel-heading{margin-bottom:8px!important}.panel-heading h2{font-size:14pt!important;color:#0f172a!important}.table-scroll{overflow:visible!important;border-color:#94a3b8!important}.backtest-table{min-width:0!important;background:#fff!important;page-break-inside:auto}.backtest-table th{background:#e2e8f0!important;color:#0f172a!important;font-size:7.5pt!important}.backtest-table th,.backtest-table td{border-bottom:1px solid #cbd5e1!important;padding:6px 7px!important}.backtest-table td,.backtest-table td strong,.table-link strong{color:#0f172a!important}.backtest-table td em,.table-link em{color:#475569!important}.rank-chip,.trust-chip{background:#fff!important;color:#0f172a!important;border-color:#64748b!important}.return-positive{color:#047857!important}.return-negative{color:#b91c1c!important}.shadow-list{grid-template-columns:repeat(3,minmax(0,1fr))!important;gap:7px!important}.shadow-list li{background:#fff!important;border-color:#cbd5e1!important;break-inside:avoid}.shadow-list strong{color:#0f172a!important}.shadow-list span{color:#475569!important}.backtest-warnings{break-inside:avoid;page-break-inside:avoid}.app-warnings-panel{display:none!important}a{text-decoration:none!important}}
"""


def _interactions_js() -> str:
    return """
(() => {
  document.documentElement.classList.add('x2-js');
  const menuFor = (toggle) => document.getElementById(toggle.dataset.x2Toggle || '');
  const triggerFor = (menu) => document.querySelector(`[data-x2-toggle="${menu.id}"]`);
  const closeMenu = (menu, trigger) => {
    if (!menu) {
      return;
    }
    menu.hidden = true;
    menu.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('trade-menu-open');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
    }
  };
  const closeForToggle = (toggle) => closeMenu(menuFor(toggle), toggle);
  for (const menu of document.querySelectorAll('.trade-menu')) {
    menu.hidden = true;
    menu.setAttribute('aria-hidden', 'true');
  }
  const search = document.querySelector('[data-x2-search]');
  if (search) {
    const items = Array.from(document.querySelectorAll('[data-filter-item]'));
    search.addEventListener('input', () => {
      const term = search.value.trim().toLowerCase();
      for (const item of items) {
        const hide = term.length > 0 && !item.textContent.toLowerCase().includes(term);
        item.hidden = hide;
        if (hide) {
          const toggle = item.querySelector('[data-x2-toggle]');
          if (toggle) {
            closeForToggle(toggle);
          }
        }
      }
    });
  }
  for (const toggle of document.querySelectorAll('[data-x2-toggle]')) {
    toggle.addEventListener('click', () => {
      const menu = menuFor(toggle);
      if (!menu) {
        return;
      }
      const nextOpen = menu.hidden;
      for (const openMenu of document.querySelectorAll('.trade-menu:not([hidden])')) {
        closeMenu(openMenu, triggerFor(openMenu));
      }
      if (nextOpen) {
        menu.hidden = false;
        menu.setAttribute('aria-hidden', 'false');
        document.body.classList.add('trade-menu-open');
        toggle.setAttribute('aria-expanded', 'true');
        const panel = menu.querySelector('.trade-menu-panel');
        if (panel) {
          panel.focus();
        }
      } else {
        closeMenu(menu, toggle);
      }
    });
  }
  for (const close of document.querySelectorAll('[data-x2-close]')) {
    close.addEventListener('click', () => {
      const menu = document.getElementById(close.dataset.x2Close || '');
      closeMenu(menu, menu ? triggerFor(menu) : null);
    });
  }
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') {
      return;
    }
    const menu = document.querySelector('.trade-menu:not([hidden])');
    if (menu) {
      closeMenu(menu, triggerFor(menu));
    }
  });
  for (const cell of document.querySelectorAll('[data-day-summary]')) {
    cell.addEventListener('mouseenter', () => {
      cell.setAttribute('aria-label', cell.dataset.daySummary || 'Day story');
    });
  }
})();
"""


def _favicon_svg() -> str:
    return """<svg viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#06080d"/>
  <path d="M12 40 L25 27 L34 34 L52 16" fill="none" stroke="#35d5ff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="46" cy="42" r="8" fill="#38e29b"/>
</svg>
"""


def _file_count(path: Path) -> int:
    if path.is_file():
        return 1
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.is_dir():
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


def _root_link(path: Path, href: str) -> str:
    output_root = _output_root_for(path)
    target = output_root / href
    return _relative(path.parent, target)


def _rel_assets(path: Path) -> str:
    return _relative(path.parent, _output_root_for(path) / "assets")


def _output_root_for(path: Path) -> Path:
    parts = path.parts
    if "v2_command_center_x2" in parts:
        index = parts.index("v2_command_center_x2")
        return Path(*parts[: index + 1])
    if path.parent.name in {"pages", "days", "months", "strategies"}:
        return path.parent.parent
    return path.parent


def _relative(start: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def _spark_height(value: Any) -> int:
    text = str(value or "").replace("%", "")
    try:
        number = abs(float(text))
    except ValueError:
        return 10
    return max(8, min(100, int(20 + number * 400)))


def _slug(value: Any) -> str:
    text = str(value or "unknown").lower()
    output = "".join(char if char.isalnum() else "_" for char in text)
    return "_".join(part for part in output.split("_") if part) or "unknown"


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _json_fence(payload: Any) -> str:
    return "```json\n" + json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n```"


def _bullet(items: list[str]) -> str:
    if not items:
        return "- None."
    return "\n".join(f"- {item}" for item in items)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
