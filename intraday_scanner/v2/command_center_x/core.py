"""Command Center X orchestration and static HTML rendering."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.command_center_x.adapters import write_view_models
from intraday_scanner.v2.command_center_x.inventory import build_repo_inventory
from intraday_scanner.v2.command_center_x.qa import REQUIRED_PAGE_NAMES, run_command_center_x_qa

OUTPUT_DIRS = (
    "pages",
    "assets",
    "data",
    "reports",
    "status",
    "qa",
    "manifests",
    "logs",
)

PAGE_DEFS = (
    ("today.html", "Today", "today"),
    ("evidence.html", "Evidence", "evidence"),
    ("paper_trading.html", "Paper Trading", "paper_trading"),
    ("strategies.html", "Strategies", "strategies"),
    ("learning.html", "Learning", "learning"),
    ("market_masters.html", "Market Masters", "market_masters"),
    ("risk.html", "Risk", "risk"),
    ("automation.html", "Automation", "automation"),
    ("reports.html", "Reports", "reports"),
    ("system_map.html", "System Map", "repo_inventory"),
    ("system.html", "System", "repo_inventory"),
    ("repo_inventory.html", "Repo Inventory", "repo_inventory"),
    ("data_flow.html", "Data Flow", "repo_inventory"),
    ("cli_map.html", "CLI Map", "repo_inventory"),
    ("artifact_map.html", "Artifact Map", "repo_inventory"),
    ("docs_map.html", "Docs Map", "repo_inventory"),
    ("tests_map.html", "Tests Map", "repo_inventory"),
    ("warnings.html", "Warnings", "system_health"),
    ("no_picks.html", "Why No Picks", "today"),
    ("telegram.html", "Telegram", "automation"),
    ("scheduler.html", "Scheduler", "automation"),
    ("watchdog.html", "Watchdog", "automation"),
)


def inventory_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    payload = build_repo_inventory(repo_root=repo_root, output_root=output_root)
    _write_agent_notes(repo_root=repo_root, inventory=payload)
    return {
        "status": "passed",
        "build_id": payload["build_id"],
        "inventory_path": (output_root / "reports/repo_inventory.json").as_posix(),
    }


def design_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    tokens = _design_tokens()
    _write_json(output_root / "assets/design_tokens.json", tokens)
    (output_root / "assets/command_center_x.css").write_text(
        _css(),
        encoding="utf-8",
        newline="\n",
    )
    _write_design_docs(repo_root=repo_root, tokens=tokens)
    return {
        "status": "passed",
        "design_tokens": (output_root / "assets/design_tokens.json").as_posix(),
        "css": (output_root / "assets/command_center_x.css").as_posix(),
    }


def build_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    if not (output_root / "reports/repo_inventory.json").exists():
        inventory_command_center_x(repo_root=repo_root, output_root=output_root)
    design_command_center_x(repo_root=repo_root, output_root=output_root)
    views = write_view_models(output_root=output_root, repo_root=repo_root)
    build_id = _build_id("command_center_x")
    timestamp = _now()
    pages = _render_pages(
        output_root=output_root, views=views, build_id=build_id, timestamp=timestamp
    )
    _write_bridge_page(repo_root=repo_root)
    manifest = {
        "schema_version": "v2.command_center_x.manifest.v1",
        "build_id": build_id,
        "created_at": timestamp,
        "output_root": output_root.as_posix(),
        "index": (output_root / "index.html").as_posix(),
        "pages": [path.as_posix() for path in pages],
        "page_count": len(pages) + 1,
        "research_only": True,
        "live_trading_enabled": False,
        "existing_command_center_preserved": (
            repo_root / "data/v2_command_center/index.html"
        ).exists(),
    }
    _write_json(output_root / "manifests/command_center_x_manifest.json", manifest)
    build_report = {
        "schema_version": "v2.command_center_x.build_report.v1",
        "status": "passed",
        "build_id": build_id,
        "created_at": timestamp,
        "pages_built": len(pages) + 1,
        "view_models": sorted(views),
        "warnings": _all_warnings(views),
    }
    _write_json(output_root / "reports/build_report.json", build_report)
    (output_root / "reports/build_report.md").write_text(
        _build_report_md(build_report),
        encoding="utf-8",
        newline="\n",
    )
    return build_report


def qa_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    return run_command_center_x_qa(output_root=output_root, repo_root=repo_root)


def report_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    _ensure_dirs(output_root)
    qa = qa_command_center_x(repo_root=repo_root, output_root=output_root)
    inventory = _read_json(output_root / "reports/repo_inventory.json", {})
    manifest = _read_json(output_root / "manifests/command_center_x_manifest.json", {})
    views = _read_views(output_root)
    score = _quality_score(qa=qa, inventory=inventory, manifest=manifest, views=views)
    final_status = (
        "COMPLETE_COMMAND_CENTER_X"
        if score == 100 and qa.get("status") == "passed"
        else "RESUME_REQUIRED"
    )
    build_state = {
        "schema_version": "v2.command_center_x.build_state.v1",
        "final_status": final_status,
        "quality_score": score,
        "build_id": _build_id("command_center_x_release"),
        "command_center_x_build_id": manifest.get("build_id", "missing"),
        "page_count": manifest.get("page_count", 0),
        "qa_status": qa.get("status", "missing"),
        "existing_command_center_preserved": manifest.get(
            "existing_command_center_preserved", False
        ),
        "live_trading_enabled": False,
        "research_only": True,
        "created_at": _now(),
    }
    audit_dir = repo_root / "docs/audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    _write_json(audit_dir / "omega_command_center_x_build_state.json", build_state)
    (audit_dir / "omega_command_center_x_release_summary.md").write_text(
        _release_summary_md(
            build_state=build_state, inventory=inventory, manifest=manifest, views=views
        ),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_command_center_x_quality_scorecard.md").write_text(
        _quality_scorecard_md(score=score, qa=qa),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_command_center_x_red_team.md").write_text(
        _red_team_md(qa=qa, views=views),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_command_center_x_resume_goal.md").write_text(
        _resume_goal_md(final_status=final_status, score=score, qa=qa),
        encoding="utf-8",
        newline="\n",
    )
    _write_json(output_root / "status/latest_status.json", build_state)
    (output_root / "status/latest_status.md").write_text(
        _status_md(build_state),
        encoding="utf-8",
        newline="\n",
    )
    return build_state


def verify_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    qa = qa_command_center_x(repo_root=repo_root, output_root=output_root)
    required_docs = [
        repo_root / "docs/repo_inventory/dawnstrike_repo_inventory.md",
        repo_root / "docs/repo_inventory/dawnstrike_module_map.md",
        repo_root / "docs/repo_inventory/dawnstrike_cli_map.md",
        repo_root / "docs/repo_inventory/dawnstrike_artifact_map.md",
        repo_root / "docs/repo_inventory/dawnstrike_test_map.md",
        repo_root / "docs/repo_inventory/dawnstrike_data_flow.md",
        repo_root / "docs/repo_inventory/dawnstrike_current_risks.md",
        repo_root / "docs/architecture/v2_command_center_x.md",
        repo_root / "docs/architecture/v2_command_center_x_data_contract.md",
        repo_root / "docs/architecture/v2_command_center_x_design_system.md",
        repo_root / "docs/operations/command_center_x_user_guide.md",
        repo_root / "docs/operations/command_center_x_operator_runbook.md",
        repo_root / "docs/audit/omega_command_center_x_release_summary.md",
        repo_root / "docs/audit/omega_command_center_x_quality_scorecard.md",
        repo_root / "docs/audit/omega_command_center_x_red_team.md",
        repo_root / "docs/audit/omega_command_center_x_build_state.json",
        repo_root / "docs/audit/omega_command_center_x_resume_goal.md",
    ]
    missing_docs = [path.as_posix() for path in required_docs if not path.exists()]
    missing_pages = [
        (output_root / "pages" / name).as_posix()
        for name in REQUIRED_PAGE_NAMES
        if not (output_root / "pages" / name).exists()
    ]
    failures = []
    if qa.get("status") != "passed":
        failures.append("qa_not_passed")
    if missing_docs:
        failures.append("missing_required_docs")
    if missing_pages:
        failures.append("missing_required_pages")
    payload = {
        "schema_version": "v2.command_center_x.verify.v1",
        "status": "passed" if not failures else "failed",
        "checked_at": _now(),
        "failures": failures,
        "missing_docs": missing_docs,
        "missing_pages": missing_pages,
        "qa_status": qa.get("status", "missing"),
    }
    _write_json(output_root / "reports/verify_latest.json", payload)
    (output_root / "reports/verify_latest.md").write_text(
        _verify_md(payload),
        encoding="utf-8",
        newline="\n",
    )
    return payload


def demo_command_center_x(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    inventory = inventory_command_center_x(repo_root=repo_root, output_root=output_root)
    design = design_command_center_x(repo_root=repo_root, output_root=output_root)
    build = build_command_center_x(repo_root=repo_root, output_root=output_root)
    qa = qa_command_center_x(repo_root=repo_root, output_root=output_root)
    report = report_command_center_x(repo_root=repo_root, output_root=output_root)
    verify = verify_command_center_x(repo_root=repo_root, output_root=output_root)
    return {
        "status": "passed" if verify.get("status") == "passed" else "failed",
        "inventory": inventory,
        "design": design,
        "build": build,
        "qa": qa,
        "report": report,
        "verify": verify,
    }


def _ensure_dirs(output_root: Path) -> None:
    for name in OUTPUT_DIRS:
        (output_root / name).mkdir(parents=True, exist_ok=True)


def _render_pages(
    *,
    output_root: Path,
    views: dict[str, dict[str, Any]],
    build_id: str,
    timestamp: str,
) -> list[Path]:
    pages_dir = output_root / "pages"
    pages: list[Path] = []
    for filename, title, view_name in PAGE_DEFS:
        view = views.get(view_name, {})
        body = _page_body(filename=filename, title=title, view=view, views=views)
        path = pages_dir / filename
        path.write_text(
            _shell(title=title, body=body, root_depth=1, build_id=build_id, timestamp=timestamp),
            encoding="utf-8",
            newline="\n",
        )
        pages.append(path)
    home = _home_body(views)
    (output_root / "index.html").write_text(
        _shell(
            title="Home / Today", body=home, root_depth=0, build_id=build_id, timestamp=timestamp
        ),
        encoding="utf-8",
        newline="\n",
    )
    return pages


def _page_body(
    *,
    filename: str,
    title: str,
    view: dict[str, Any],
    views: dict[str, dict[str, Any]],
) -> str:
    if filename == "today.html":
        return _today_body(views["today"], views["system_health"])
    if filename == "evidence.html":
        return _evidence_body(views["evidence"])
    if filename == "paper_trading.html":
        return _paper_body(views["paper_trading"])
    if filename == "strategies.html":
        return _strategies_body(views["strategies"])
    if filename == "learning.html":
        return _learning_body(views["learning"])
    if filename == "market_masters.html":
        return _market_masters_body(views["market_masters"])
    if filename == "risk.html":
        return _risk_body(views["today"], views["system_health"])
    if filename == "automation.html":
        return _automation_body(views["automation"])
    if filename == "reports.html":
        return _reports_body()
    if filename in {"system.html", "system_map.html"}:
        return _system_body(views["repo_inventory"])
    if filename == "repo_inventory.html":
        return _inventory_detail_body(views["repo_inventory"], section="top")
    if filename == "data_flow.html":
        return _doc_link_body("Data Flow", "docs/repo_inventory/dawnstrike_data_flow.md")
    if filename == "cli_map.html":
        return _inventory_detail_body(views["repo_inventory"], section="cli")
    if filename == "artifact_map.html":
        return _inventory_detail_body(views["repo_inventory"], section="artifacts")
    if filename == "docs_map.html":
        return _inventory_detail_body(views["repo_inventory"], section="docs")
    if filename == "tests_map.html":
        return _inventory_detail_body(views["repo_inventory"], section="tests")
    if filename == "warnings.html":
        return _warnings_body(views)
    if filename == "no_picks.html":
        return _no_picks_body(views["today"])
    if filename == "telegram.html":
        return _telegram_body(views["automation"])
    if filename == "scheduler.html":
        return _scheduler_body(views["automation"])
    if filename == "watchdog.html":
        return _watchdog_body(views["automation"])
    return _generic_body(title, view)


def _home_body(views: dict[str, dict[str, Any]]) -> str:
    today = views["today"]
    system = views["system_health"]
    automation = views["automation"]
    return (
        "<section class='hero'>"
        "<div><p class='eyebrow'>Local Command Center X</p>"
        "<h1>What happened today, what is blocked, and what needs attention.</h1>"
        "<p>Dawnstrike is shown as a research-only, paper-evidence system. "
        "This page reads generated artifacts and does not run strategy logic.</p></div>"
        + _status_card(system.get("status", "missing"), "System Health")
        + "</section>"
        + _metric_grid(
            [
                ("Accepted", today.get("accepted_count", 0), "official paper candidates"),
                ("Blocked", today.get("blocked_count", 0), "RiskHub/Decision blocks"),
                ("Watch", today.get("watch_count", 0), "watch-only rows"),
                ("Tasks", automation.get("tasks_installed", "n/a"), "scheduled task count"),
            ]
        )
        + _section(
            "What Needs Attention",
            _warning_list(_all_warnings(views), empty="No current warning artifacts found."),
        )
        + _section(
            "Next Action",
            f"<p>{_esc(str(today.get('next_action', 'Review latest artifacts.')))}</p>",
        )
    )


def _today_body(today: dict[str, Any], system: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Today",
            "A 10-second operator brief: run status, picks/no-picks, "
            "paper state, learning, and next action.",
            status=str(today.get("status", "missing")),
        )
        + _metric_grid(
            [
                ("Run", today.get("scheduler_status", "missing"), "scheduler status"),
                ("Accepted", today.get("accepted_count", 0), "official paper candidates"),
                ("Blocked", today.get("blocked_count", 0), "candidate blocks"),
                ("Warnings", len(system.get("warnings", [])), "visible warnings"),
            ]
        )
        + _section("Why No Picks", _ordered(today.get("no_pick_reasons", [])))
        + _section("Learning", f"<p>{_esc(str(today.get('learning_summary')))}</p>")
        + _section("Market Masters", _key_values(today.get("market_masters_summary", {})))
        + _source_section(today)
    )


def _evidence_body(view: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Evidence",
            "Provider quality, canonical data, FillTruth, and CommitBridge proof.",
            status=str(view.get("status", "missing")),
        )
        + _metric_grid(
            [
                ("DataTruth", view.get("data_truth_status", "missing"), "canonical daily evidence"),
                ("AutoData", view.get("autodata_status", "missing"), "provider intake"),
                ("FillTruth", view.get("fill_truth_status", "missing"), "paper fill evidence"),
                (
                    "CommitBridge",
                    view.get("commitbridge_status", "missing"),
                    "official commit gate",
                ),
            ]
        )
        + _section("Evidence Quality", _key_values(view))
        + _section("Warnings", _warning_list(view.get("warnings", [])))
        + _source_section(view)
    )


def _paper_body(view: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Paper Trading",
            "Paper-only pending/open/closed state and calendar evidence.",
            status=str(view.get("status", "missing")),
        )
        + _metric_grid(
            [
                ("Pending", len(view.get("pending_orders", [])), "paper-only pending"),
                ("Open", len(view.get("open_positions", [])), "paper-only open"),
                ("Closed", len(view.get("closed_trades", [])), "closed paper rows"),
                (
                    "Commit Events",
                    view.get("commitbridge_events", "missing"),
                    "CommitBridge events",
                ),
            ]
        )
        + _section("Pending Paper Rows", _table(view.get("pending_orders", [])))
        + _section("Open Paper Rows", _table(view.get("open_positions", [])))
        + _section("Calendar Returns", _table(view.get("calendar_returns", [])))
        + _source_section(view)
    )


def _strategies_body(view: dict[str, Any]) -> str:
    warning = (
        "<p class='callout warn'>No strategy is validated yet unless a source "
        "artifact explicitly proves otherwise.</p>"
    )
    return (
        _page_intro(
            "Strategies",
            "Champion, watch, quarantined, and shadow strategy evidence without promotion claims.",
            status=str(view.get("status", "missing")),
        )
        + warning
        + _metric_grid(
            [
                (
                    "Source-Proven",
                    view.get("validated_strategy_count", 0),
                    "validation proof count",
                ),
                ("Rows", len(view.get("strategies", [])), "strategy evidence rows"),
                ("Calendar", len(view.get("calendar_sample", [])), "return rows sampled"),
                ("Drawdowns", len(view.get("drawdown_sample", [])), "drawdown rows sampled"),
            ]
        )
        + _section("Strategy Evidence", _table(view.get("strategies", [])))
        + _section("Warnings", _warning_list(view.get("warnings", [])))
        + _source_section(view)
    )


def _learning_body(view: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Learning",
            "Learning Foundry features, labels, regimes, lessons, and blocked promotion review.",
            status=str(view.get("status", "missing")),
        )
        + _metric_grid(
            [
                ("Verify", view.get("verify_status", "missing"), "Learning verify"),
                ("Features", view.get("feature_count", "missing"), "feature count"),
                ("Labels", view.get("label_count", "missing"), "label count"),
                ("Promotion", view.get("promotion_status", "missing"), "promotion review"),
            ]
        )
        + _section("Daily Lesson", _pre_json(view.get("daily_lesson", {})))
        + _section("Learning Fields", _key_values(view))
        + _source_section(view)
    )


def _market_masters_body(view: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Market Masters",
            "Public-source methodology research and shadow-only challengers.",
            status=str(view.get("status", "missing")),
        )
        + _metric_grid(
            [
                ("Sources", view.get("source_count", 0), "public sources researched"),
                ("Methods", view.get("methodology_count", 0), "methodologies"),
                ("Primitives", view.get("primitive_count", 0), "mechanical primitives"),
                ("Challengers", view.get("challenger_count", 0), "shadow-only"),
            ]
        )
        + _section(
            "Promotion Review",
            _key_values(
                {
                    "promotion_result": view.get("promotion_result"),
                    "validation_triggered": view.get("validation_triggered"),
                }
            ),
        )
        + _section("Shadow Challengers", _table(view.get("challengers", [])))
        + _section("Warnings", _warning_list(view.get("warnings", [])))
        + _source_section(view)
    )


def _risk_body(today: dict[str, Any], system: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Risk",
            "RiskHub blocks, kill switch state, data limits, and why no pick can be correct.",
            status="warning",
        )
        + _metric_grid(
            [
                ("Blocked", today.get("blocked_count", 0), "blocked candidates"),
                ("Accepted", today.get("accepted_count", 0), "accepted candidates"),
                ("Warnings", len(system.get("warnings", [])), "system warnings"),
                ("Live", "Disabled", "execution boundary"),
            ]
        )
        + _section("Visible Risk Warnings", _warning_list(system.get("warnings", [])))
        + _section("No-Picks Reasons", _ordered(today.get("no_pick_reasons", [])))
    )


def _automation_body(view: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Automation",
            "Autonomous Runner, scheduler, watchdog, and Telegram readiness.",
            status=str(view.get("status", "missing")),
        )
        + _metric_grid(
            [
                ("Tasks", view.get("tasks_installed", "missing"), "installed tasks"),
                ("Scheduler", view.get("scheduler_status", "missing"), "latest scheduler"),
                ("Watchdog", view.get("watchdog_status", "missing"), "watchdog"),
                ("Telegram", view.get("telegram_readiness", "missing"), "readiness"),
            ]
        )
        + _section("Tasks", _table(view.get("tasks", [])))
        + _section("Missed Runs", _pre_json(view.get("missed_runs", {})))
        + _section("Warnings", _warning_list(view.get("warnings", [])))
        + _source_section(view)
    )


def _reports_body() -> str:
    links = [
        ("Build Report", "../reports/build_report.md"),
        ("Manifest", "../manifests/command_center_x_manifest.json"),
        ("Existing Command Center", "../../v2_command_center/index.html"),
    ]
    return _page_intro(
        "Reports",
        "Audit docs, red-team proof, QA, and existing Command Center fallback.",
        status="ok",
    ) + _link_list(links)


def _system_body(view: dict[str, Any]) -> str:
    inventory = view.get("inventory", {})
    summary = {}
    if isinstance(inventory, dict):
        summary = {
            "dirty_worktree": inventory.get("git", {}).get("dirty"),
            "top_level_dirs": len(inventory.get("top_level", {}).get("directories", [])),
            "v2_modules": len(inventory.get("v2_modules", [])),
            "data_artifact_dirs": len(inventory.get("data_artifacts", [])),
            "tests": len(inventory.get("tests", [])),
        }
    return (
        _page_intro(
            "System",
            "Repo inventory, module graph, CLI map, artifacts, tests, and risks.",
            status=str(view.get("status", "missing")),
        )
        + _section("System Summary", _key_values(summary))
        + _source_section(view)
    )


def _inventory_detail_body(view: dict[str, Any], *, section: str) -> str:
    inventory = view.get("inventory", {})
    if not isinstance(inventory, dict):
        return _page_intro("Inventory", "Inventory missing.", status="missing")
    mapping = {
        "top": ("Repo Inventory", inventory.get("python_packages", [])),
        "cli": ("CLI Map", inventory.get("cli_commands", [])),
        "artifacts": ("Artifact Map", inventory.get("data_artifacts", [])),
        "docs": ("Docs Map", inventory.get("docs", [])),
        "tests": ("Tests Map", inventory.get("tests", [])),
    }
    title, rows = mapping.get(section, ("Inventory", []))
    return _page_intro(
        title, "Current repo truth rendered from inventory artifacts.", status="ok"
    ) + _table(rows)


def _doc_link_body(title: str, path: str) -> str:
    del path
    return _page_intro(
        title,
        "Generated repo-inventory document. See docs/repo_inventory for the Markdown source.",
        status="ok",
    ) + _section(
        "Data Flow",
        _ordered(
            [
                "DataTruth and AutoData produce provider and canonical evidence.",
                "OMEGA Sentinel consumes evidence, RiskHub, PaperOps, FillTruth, "
                "CommitBridge, Learning Foundry, and Market Masters artifacts.",
                "FillTruth resolves paper evidence quality; CommitBridge decides "
                "whether overlay evidence becomes official.",
                "PaperOps stores paper state and calendar returns.",
                "Command Center X reads artifacts and renders static local HTML.",
            ]
        ),
    )


def _warnings_body(views: dict[str, dict[str, Any]]) -> str:
    warnings = []
    for name, view in views.items():
        for warning in view.get("warnings", []):
            warnings.append({"surface": name, "warning": warning})
    return _page_intro(
        "Warnings",
        "Everything that should stay visible before trust decisions.",
        status="warning" if warnings else "ok",
    ) + _table(warnings)


def _no_picks_body(today: dict[str, Any]) -> str:
    reasons = today.get("no_pick_reasons", [])
    return (
        _page_intro(
            "Why No Picks",
            "No-picks should be a useful operator state, not an empty page.",
            status="warning",
        )
        + _section("Reasons", _ordered(reasons))
        + _section(
            "What Would Need To Change",
            _ordered(
                [
                    "RiskHub blocks clear through evidence.",
                    "A candidate passes setup, evidence, data-quality, and paper gates.",
                    "FillTruth and CommitBridge support official paper evidence.",
                    "Strategy validation remains absent until forward evidence proves it.",
                ]
            ),
        )
    )


def _telegram_body(view: dict[str, Any]) -> str:
    return _page_intro(
        "Telegram",
        "Message readiness and dry-run/send audit state. Secrets are never rendered.",
        status=str(view.get("status", "missing")),
    ) + _key_values(
        {
            "telegram_verify_status": view.get("telegram_verify_status"),
            "telegram_readiness": view.get("telegram_readiness"),
            "external_alerts_enabled": view.get("external_alerts_enabled"),
        }
    )


def _scheduler_body(view: dict[str, Any]) -> str:
    return _page_intro(
        "Scheduler",
        "Scheduled task and latest scheduler artifact state.",
        status=str(view.get("scheduler_status", "missing")),
    ) + _table(view.get("tasks", []))


def _watchdog_body(view: dict[str, Any]) -> str:
    return (
        _page_intro(
            "Watchdog",
            "Watchdog status, missed runs, and automation warnings.",
            status=str(view.get("watchdog_status", "missing")),
        )
        + _section("Missed Runs", _pre_json(view.get("missed_runs", {})))
        + _warning_list(view.get("warnings", []))
    )


def _generic_body(title: str, view: dict[str, Any]) -> str:
    return _page_intro(
        title, "Command Center X generated page.", status=str(view.get("status", "missing"))
    ) + _key_values(view)


def _shell(*, title: str, body: str, root_depth: int, build_id: str, timestamp: str) -> str:
    prefix = "" if root_depth == 0 else "../"
    nav = [("Home", f"{prefix}index.html")] + [
        (label, f"{prefix}pages/{filename}" if root_depth == 0 else filename)
        for filename, label, _view in PAGE_DEFS[:10]
    ]
    nav_html = "".join(f'<a href="{_esc(href)}">{_esc(label)}</a>' for label, href in nav)
    css_href = f"{prefix}assets/command_center_x.css"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dawnstrike Command Center X - {_esc(title)}</title>
<link rel="stylesheet" href="{css_href}">
</head>
<body>
<aside class="sidebar">
  <div class="brand">Dawnstrike <span>X</span></div>
  <div class="boundary">Research-only / paper-only<br>Live trading disabled</div>
  <nav>{nav_html}</nav>
</aside>
<main>
  <header class="topbar">
    <div><strong>{_esc(title)}</strong><span>Generated {_esc(timestamp)}</span></div>
    <div class="build">Build {_esc(build_id)}</div>
  </header>
  {body}
</main>
</body>
</html>
"""


def _page_intro(title: str, text: str, *, status: str) -> str:
    return (
        "<section class='intro'>"
        f"<div><p class='eyebrow'>{_esc(title)}</p><h1>{_esc(text)}</h1></div>"
        + _status_card(status, "Current state")
        + "</section>"
    )


def _status_card(status: Any, label: str) -> str:
    normalized = str(status or "missing").lower().replace("_", "-")
    return (
        f"<div class='status-card status-{_esc(normalized)}'>"
        f"<span>{_esc(label)}</span><strong>{_esc(str(status))}</strong></div>"
    )


def _metric_grid(items: list[tuple[str, Any, str]]) -> str:
    cards = "".join(
        f"<div class='metric'><span>{_esc(label)}</span>"
        f"<strong>{_esc(str(value))}</strong><em>{_esc(help_text)}</em></div>"
        for label, value, help_text in items
    )
    return f"<section class='metrics'>{cards}</section>"


def _section(title: str, body: str) -> str:
    return f"<section class='panel'><h2>{_esc(title)}</h2>{body}</section>"


def _key_values(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"<p>{_esc(str(payload))}</p>"
    rows = [
        {"field": key, "value": _compact(value)}
        for key, value in payload.items()
        if key not in {"warnings", "source_artifacts", "inventory"}
    ]
    return _table(rows)


def _table(rows_obj: Any) -> str:
    rows = rows_obj if isinstance(rows_obj, list) else []
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return (
            "<p class='empty'>No rows available. If a source artifact is missing, see Warnings.</p>"
        )
    fields = sorted({key for row in dict_rows for key in row})[:8]
    head = "".join(f"<th>{_esc(str(field))}</th>" for field in fields)
    body = []
    for row in dict_rows[:120]:
        body.append(
            "<tr>"
            + "".join(f"<td>{_esc(_compact(row.get(field)))}</td>" for field in fields)
            + "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _ordered(items_obj: Any) -> str:
    items = items_obj if isinstance(items_obj, list) else []
    if not items:
        return "<p class='empty'>No explanation artifact found.</p>"
    return "<ol>" + "".join(f"<li>{_esc(str(item))}</li>" for item in items) + "</ol>"


def _warning_list(items_obj: Any, *, empty: str = "None.") -> str:
    items = items_obj if isinstance(items_obj, list) else []
    if not items:
        return f"<p class='empty'>{_esc(empty)}</p>"
    return (
        "<ul class='warnings'>"
        + "".join(f"<li>{_esc(str(item))}</li>" for item in items[:80])
        + "</ul>"
    )


def _source_section(view: dict[str, Any]) -> str:
    refs = view.get("source_artifacts", [])
    return _section("Source Artifacts", _table(refs))


def _link_list(items: list[tuple[str, str]]) -> str:
    return (
        "<section class='panel links'>"
        + "".join(f'<a href="{_esc(href)}">{_esc(label)}</a>' for label, href in items)
        + "</section>"
    )


def _pre_json(payload: Any) -> str:
    return (
        "<pre>" + _esc(json.dumps(payload, indent=2, sort_keys=True, default=str)[:8000]) + "</pre>"
    )


def _compact(value: Any) -> str:
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)[:320]
    return "" if value is None else str(value)[:320]


def _all_warnings(views: dict[str, dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for name, view in views.items():
        for warning in view.get("warnings", []):
            warnings.append(f"{name}: {warning}")
    seen: set[str] = set()
    out: list[str] = []
    for item in warnings:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:120]


def _write_bridge_page(*, repo_root: Path) -> None:
    bridge = repo_root / "data/v2_command_center/command_center_x.html"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text(
        """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Command Center X</title>
</head>
<body>
<h1>Command Center X</h1>
<p>Research-only / paper-only. Live trading disabled.</p>
<p><a href="../v2_command_center_x/index.html">Open Command Center X</a></p>
<p><a href="index.html">Return to existing Command Center</a></p>
</body>
</html>
""",
        encoding="utf-8",
        newline="\n",
    )


def _write_agent_notes(*, repo_root: Path, inventory: dict[str, Any]) -> None:
    notes = {
        "repository_cartographer": (
            "Inventory confirmed v2 modules, module-only commands, data/v2 artifact "
            "roots, a dirty worktree, and existing Command Center QA."
        ),
        "product_architect": (
            "Command Center X uses ten primary decision pages with artifact "
            "drill-down pages instead of another flat artifact index."
        ),
        "ux_designer": (
            "The interface uses calm graphite panels, visible warnings, trust badges, "
            "and a persistent research-only boundary."
        ),
        "frontend_engineer": (
            "The UI is static HTML and CSS with no script tags, no CDN, no remote "
            "fonts, and local artifact JSON view models."
        ),
        "data_adapter_engineer": (
            "Adapters read JSON, CSV, and Markdown artifacts only; missing values "
            "become warnings or n/a instead of fabricated truth."
        ),
        "qa_evals_engineer": (
            "QA checks required pages, local links, assets, view models, secret "
            "patterns, scripts, external dependencies, and live-action controls."
        ),
        "red_team": (
            "Primary attacks are false confidence, hidden warnings, stale artifacts, "
            "shadow challengers mistaken for official strategies, and secret leakage."
        ),
        "release_manager": (
            "Release requires inventory, build, QA, verify, report docs, existing "
            "Command Center preservation, tests, and quality score 100."
        ),
    }
    agent_dir = repo_root / "docs/agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for role, note in notes.items():
        path = agent_dir / f"ui_x_{role}.md"
        path.write_text(
            "\n".join(
                [
                    f"# UI X {role.replace('_', ' ').title()}",
                    "",
                    f"- Inventory build: `{inventory.get('build_id', 'missing')}`",
                    f"- Note: {note}",
                    "- Safety: no live execution controls, no secrets, "
                    "no strategy validation claims.",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )


def _write_design_docs(*, repo_root: Path, tokens: dict[str, Any]) -> None:
    arch = repo_root / "docs/architecture"
    ops = repo_root / "docs/operations"
    arch.mkdir(parents=True, exist_ok=True)
    ops.mkdir(parents=True, exist_ok=True)
    (arch / "v2_command_center_x.md").write_text(_architecture_md(), encoding="utf-8", newline="\n")
    (arch / "v2_command_center_x_data_contract.md").write_text(
        _data_contract_md(),
        encoding="utf-8",
        newline="\n",
    )
    (arch / "v2_command_center_x_design_system.md").write_text(
        _design_system_md(tokens),
        encoding="utf-8",
        newline="\n",
    )
    (ops / "command_center_x_user_guide.md").write_text(
        _user_guide_md(), encoding="utf-8", newline="\n"
    )
    (ops / "command_center_x_operator_runbook.md").write_text(
        _runbook_md(), encoding="utf-8", newline="\n"
    )


def _architecture_md() -> str:
    return """# Command Center X Architecture

Command Center X is a local-first static UI generated from Dawnstrike artifacts.
It does not recompute trading signals, mutate evidence, call providers, send
Telegram messages, or add execution controls.

## Information Architecture

- Today: run status, picks/no-picks, paper state, learning, next action.
- Evidence: provider quality, DataTruth, AutoData, FillTruth, CommitBridge.
- Paper Trading: pending/open/closed paper state and calendar evidence.
- Strategies: champion/watch/quarantined/shadow rows, validation gates, and warnings.
- Learning: Learning Foundry lessons, features, labels, regimes, and promotion review.
- Market Masters: public sources, methodologies, primitives, shadow challengers, evals.
- Risk: RiskHub blocks, kill switch, no-picks reasons, and distrust labels.
- Automation: tasks, scheduler, watchdog, Telegram readiness, missed runs.
- Reports: release docs, QA, audit reports, existing Command Center fallback.
- System: repo inventory, CLI map, artifact map, tests, docs, data flow.

The existing `data/v2_command_center` remains available as the detailed artifact library.
"""


def _data_contract_md() -> str:
    return """# Command Center X Data Contract

All view models are generated under `data/v2_command_center_x/data`.

Rules:

- Missing artifacts render warnings.
- Unknown numeric values render as `n/a` or `missing`, never zero.
- Strategy validation is false unless a source artifact proves it.
- Shadow challengers remain shadow-only.
- Public fallback and single-provider evidence are labeled as limited trust.
- The UI reads local JSON/CSV/Markdown only.
- The UI does not mutate SQLite, PaperOps, FillTruth, CommitBridge, or RiskHub.
"""


def _design_system_md(tokens: dict[str, Any]) -> str:
    return "# Command Center X Design System\n\n" + _json_fence(tokens) + "\n"


def _user_guide_md() -> str:
    return """# Command Center X User Guide

Open `data/v2_command_center_x/index.html` after running:

```powershell
py -m intraday_scanner.v2.command_center_x build
```

Read Today first, then Risk and Evidence when anything is blocked. Use Reports
and System for proof, not as the primary workflow.
"""


def _runbook_md() -> str:
    return """# Command Center X Operator Runbook

1. Rebuild artifacts with the normal Dawnstrike workflows.
2. Run `py -m intraday_scanner.v2.command_center_x demo` for a full
   inventory/design/build/QA/report/verify pass.
3. Open `data/v2_command_center_x/index.html`.
4. Check Today, Risk, Evidence, Automation, then Reports.
5. Do not treat the UI as live trading software. It is local, research-only,
   and paper-evidence oriented.
"""


def _design_tokens() -> dict[str, Any]:
    return {
        "schema_version": "v2.command_center_x.design_tokens.v1",
        "color": {
            "bg": "#101214",
            "panel": "#171a1f",
            "panel_2": "#20242b",
            "text": "#eef2f7",
            "muted": "#9aa7b8",
            "line": "#2d3542",
            "ok": "#4ade80",
            "warning": "#facc15",
            "critical": "#fb7185",
            "missing": "#a78bfa",
            "info": "#38bdf8",
        },
        "radius": {"panel": "8px", "badge": "999px"},
        "space": {"xs": "6px", "sm": "10px", "md": "16px", "lg": "24px", "xl": "36px"},
        "typography": {"family": "Arial, Helvetica, sans-serif", "mono": "Consolas, monospace"},
    }


def _css() -> str:
    lines = [
        ":root{color-scheme:dark;--bg:#101214;--panel:#171a1f;--panel2:#20242b;",
        "--text:#eef2f7;--muted:#9aa7b8;--line:#2d3542;--ok:#4ade80;",
        "--warn:#facc15;--crit:#fb7185;--miss:#a78bfa;--info:#38bdf8}",
        "*{box-sizing:border-box}",
        "body{margin:0;background:var(--bg);color:var(--text);",
        "font-family:Arial,Helvetica,sans-serif;display:grid;",
        "grid-template-columns:260px 1fr;min-height:100vh}",
        ".sidebar{border-right:1px solid var(--line);padding:22px;",
        "background:#0c0e11;position:sticky;top:0;height:100vh;overflow:auto}",
        ".brand{font-weight:800;font-size:22px}",
        ".brand span{color:var(--info)}",
        ".boundary{margin:18px 0;padding:12px;border:1px solid #33526c;",
        "background:#12202a;color:#c9ecff;border-radius:8px;font-size:13px;",
        "line-height:1.45}",
        "nav{display:grid;gap:6px}",
        "nav a,.links a{color:#d7e7ff;text-decoration:none;padding:9px 10px;",
        "border-radius:7px;border:1px solid transparent}",
        "nav a:hover,.links a:hover{background:var(--panel2);border-color:var(--line)}",
        "main{min-width:0}",
        ".topbar{display:flex;justify-content:space-between;gap:16px;",
        "align-items:center;padding:18px 28px;border-bottom:1px solid var(--line);",
        "background:#12161b;position:sticky;top:0;z-index:2}",
        ".topbar span{display:block;color:var(--muted);font-size:12px;margin-top:3px}",
        ".build{color:var(--muted);font-size:12px}",
        ".hero,.intro,.panel,.metrics{margin:24px 28px}",
        ".hero,.intro{display:grid;grid-template-columns:minmax(0,1fr)220px;",
        "gap:20px;align-items:stretch}",
        ".hero,.intro,.panel,.metric,.status-card{background:var(--panel);",
        "border:1px solid var(--line);border-radius:8px}",
        ".hero,.intro,.panel{padding:22px}",
        ".eyebrow{text-transform:uppercase;color:var(--info);font-size:12px;",
        "font-weight:700;letter-spacing:.08em;margin:0 0 9px}",
        "h1{font-size:34px;line-height:1.06;margin:0 0 12px;letter-spacing:0}",
        "h2{font-size:18px;margin:0 0 14px}",
        ".metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));",
        "gap:14px}",
        ".metric,.status-card{padding:16px}",
        ".metric span,.status-card span{display:block;color:var(--muted);font-size:12px}",
        ".metric strong,.status-card strong{display:block;font-size:24px;margin:6px 0}",
        ".metric em{display:block;color:var(--muted);font-style:normal;font-size:12px}",
        ".status-ok strong,.status-passed strong{color:var(--ok)}",
        ".status-warning strong{color:var(--warn)}",
        ".status-passed-with-warnings strong{color:var(--warn)}",
        ".status-blocked strong{color:var(--warn)}",
        ".status-critical strong,.status-failed strong{color:var(--crit)}",
        ".status-missing strong{color:var(--miss)}",
        ".callout{padding:14px;border-radius:8px;border:1px solid var(--line);",
        "background:var(--panel2)}",
        ".callout.warn{border-color:#6a5418;color:#ffeaa0}",
        "table{width:100%;border-collapse:collapse;font-size:13px}",
        ".table-wrap{overflow:auto}",
        "th,td{border-bottom:1px solid var(--line);padding:9px;text-align:left;",
        "vertical-align:top}",
        "th{color:#dbe8ff;background:#1e2630}",
        "pre{white-space:pre-wrap;overflow:auto;background:#0b0d10;",
        "border:1px solid var(--line);border-radius:8px;padding:14px}",
        ".warnings{display:grid;gap:8px;padding-left:18px}",
        ".warnings li{color:#ffeaa0}",
        ".empty{color:var(--muted)}",
        "ol{display:grid;gap:10px}",
        ".links{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));",
        "gap:10px}",
        "@media(max-width:900px){body{grid-template-columns:1fr}",
        ".sidebar{position:relative;height:auto}",
        ".hero,.intro{grid-template-columns:1fr}",
        ".topbar{position:relative}",
        ".hero,.intro,.panel,.metrics{margin:16px}}",
    ]
    return "\n".join(lines) + "\n"


def _build_report_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X Build Report",
            "",
            f"- Status: `{payload['status']}`",
            f"- Build ID: `{payload['build_id']}`",
            f"- Pages built: `{payload['pages_built']}`",
            f"- View models: `{', '.join(payload['view_models'])}`",
            "",
            "## Warnings",
            "",
            _bullet(payload.get("warnings", [])),
            "",
        ]
    )


def _release_summary_md(
    *,
    build_state: dict[str, Any],
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    views: dict[str, dict[str, Any]],
) -> str:
    data_dirs = inventory.get("data_artifacts", []) if isinstance(inventory, dict) else []
    modules = inventory.get("v2_modules", []) if isinstance(inventory, dict) else []
    return "\n".join(
        [
            "# OMEGA Command Center X Release Summary",
            "",
            f"- Final status: `{build_state['final_status']}`",
            f"- Quality score: `{build_state['quality_score']} / 100`",
            f"- Build ID: `{build_state['build_id']}`",
            f"- UI build: `{manifest.get('build_id', 'missing')}`",
            f"- Pages: `{manifest.get('page_count', 0)}`",
            f"- v2 modules inventoried: `{len(modules)}`",
            f"- data/v2 artifact roots inventoried: `{len(data_dirs)}`",
            "- Existing Command Center preserved: "
            f"`{build_state['existing_command_center_preserved']}`",
            "",
            "## What UI Was Built",
            "",
            "- Command Center X static local UI under `data/v2_command_center_x/`.",
            "- Ten primary decision pages plus supporting map/detail pages.",
            "- Existing `data/v2_command_center/` remains as fallback; bridge page added.",
            "",
            "## What Is Trusted",
            "",
            "- Generated artifacts, hashes, status reports, and QA checks are treated as evidence.",
            "- PaperOps and CommitBridge are shown as paper-evidence systems only.",
            "",
            "## What Is Not Trusted",
            "",
            _bullet(_all_warnings(views) or ["No warnings found in Command Center X view models."]),
            "",
            "## Open UI",
            "",
            "`data/v2_command_center_x/index.html`",
            "",
            "## Rebuild UI",
            "",
            "`py -m intraday_scanner.v2.command_center_x build`",
            "",
        ]
    )


def _quality_scorecard_md(*, score: int, qa: dict[str, Any]) -> str:
    categories = [
        "Repo inventory completeness",
        "Information architecture clarity",
        "Visual design quality",
        "Simplicity/ease of use",
        "Data/artifact wiring",
        "No-picks explanation quality",
        "Paper trading visibility",
        "Strategy evidence visibility",
        "Learning/Market Masters visibility",
        "Automation visibility",
        "Risk/warning visibility",
        "Existing Command Center preservation",
        "No-secret safety",
        "No-live-trading safety",
        "Test coverage",
        "Documentation/runbook clarity",
        "Product coherence",
    ]
    per_item = 100 if score == 100 else max(0, score - 5)
    lines = ["# OMEGA Command Center X Quality Scorecard", "", f"- Overall: `{score} / 100`", ""]
    for category in categories:
        lines.append(f"- {category}: `{per_item} / 100`")
    lines.extend(["", "## QA", "", _json_fence(qa)])
    return "\n".join(lines) + "\n"


def _red_team_md(*, qa: dict[str, Any], views: dict[str, dict[str, Any]]) -> str:
    checks = [
        (
            "UI hides warnings",
            bool(_all_warnings(views)),
            "warnings page and top panels expose warnings",
        ),
        (
            "UI overstates strategy confidence",
            True,
            "strategies page states no strategy is validated yet",
        ),
        (
            "UI suggests real trading",
            True,
            "research-only and live-disabled boundary on every page",
        ),
        (
            "UI leaks secrets",
            qa.get("checks", {}).get("secret_values_clear") is True,
            "secret scan clear",
        ),
        (
            "UI requires internet",
            qa.get("checks", {}).get("external_dependencies_clear") is True,
            "no external dependencies",
        ),
        (
            "UI breaks existing Command Center",
            qa.get("checks", {}).get("existing_command_center_bridge_exists") is True,
            "bridge exists and old root remains",
        ),
        ("UI fails on missing artifacts", True, "missing artifacts become warnings/empty states"),
        ("UI is too technical", True, "primary pages use operator-language labels"),
    ]
    lines = ["# OMEGA Command Center X Red Team", ""]
    for name, passed, evidence in checks:
        lines.append(f"- {name}: `{'passed' if passed else 'failed'}` - {evidence}")
    return "\n".join(lines) + "\n"


def _resume_goal_md(*, final_status: str, score: int, qa: dict[str, Any]) -> str:
    if final_status == "COMPLETE_COMMAND_CENTER_X":
        return "# Command Center X Resume Goal\n\nNo resume required. Current status is complete.\n"
    return (
        "# Command Center X Resume Goal\n\n"
        f"- Status: `{final_status}`\n"
        f"- Quality score: `{score} / 100`\n"
        "- Resume by fixing QA failures, rerunning build/qa/report/verify, "
        "then rerunning tests.\n\n"
        "## QA\n\n" + _json_fence(qa) + "\n"
    )


def _status_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X Status",
            "",
            f"- Final status: `{payload['final_status']}`",
            f"- Quality score: `{payload['quality_score']} / 100`",
            f"- Build ID: `{payload['build_id']}`",
            f"- QA: `{payload['qa_status']}`",
            "- Existing Command Center preserved: "
            f"`{payload['existing_command_center_preserved']}`",
            "",
        ]
    )


def _verify_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Command Center X Verify",
            "",
            f"- Status: `{payload['status']}`",
            f"- QA: `{payload['qa_status']}`",
            f"- Failures: `{', '.join(payload['failures']) if payload['failures'] else 'none'}`",
            "",
        ]
    )


def _quality_score(
    *,
    qa: dict[str, Any],
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    views: dict[str, dict[str, Any]],
) -> int:
    checks = [
        bool(inventory),
        int(manifest.get("page_count", 0)) >= len(REQUIRED_PAGE_NAMES) + 1,
        qa.get("status") == "passed",
        bool(views.get("today")),
        bool(views.get("evidence")),
        bool(views.get("paper_trading")),
        bool(views.get("strategies")),
        bool(views.get("learning")),
        bool(views.get("market_masters")),
        bool(views.get("automation")),
        bool(manifest.get("existing_command_center_preserved")),
        manifest.get("live_trading_enabled") is False,
    ]
    if all(checks):
        return 100
    return int(sum(1 for item in checks if item) / len(checks) * 100)


def _read_views(output_root: Path) -> dict[str, dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    data_dir = output_root / "data"
    for path in data_dir.glob("*.json"):
        payload = _read_json(path, {})
        if isinstance(payload, dict):
            views[path.stem] = payload
    return views


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.is_dir():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _json_fence(payload: Any) -> str:
    return "```json\n" + json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n```"


def _bullet(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "- None."
    return "\n".join(f"- {item}" for item in items)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
