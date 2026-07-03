"""Command Center X2 static UI QA."""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REQUIRED_PAGE_NAMES = (
    "today.html",
    "calendar.html",
    "strategies.html",
    "no_picks.html",
    "automation.html",
    "telegram.html",
    "reports.html",
    "six_month_backtest.html",
    "day_trade_lab.html",
    "day_trade_calendar.html",
    "day_trade_strategies.html",
    "day_trade_trades.html",
    "day_trade_no_trade_days.html",
    "day_trade_assumptions.html",
    "day_trade_robustness.html",
    "day_trade_slippage_stress.html",
    "day_trade_oos.html",
    "day_trade_refinements.html",
    "system_map.html",
)

REQUIRED_ASSETS = (
    "x2_design_tokens.json",
    "x2.css",
    "x2_components.css",
    "x2_interactions.js",
)


def run_command_center_x2_qa(
    *,
    output_root: Path = Path("data/v2_command_center_x2"),
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    pages_dir = output_root / "pages"
    assets_dir = output_root / "assets"
    html_files = _html_files(output_root)
    texts = {
        path.as_posix(): path.read_text(encoding="utf-8") for path in html_files if path.exists()
    }
    required_pages = [pages_dir / name for name in REQUIRED_PAGE_NAMES]
    missing_pages = [path.as_posix() for path in required_pages if not path.exists()]
    missing_assets = [
        (assets_dir / name).as_posix()
        for name in REQUIRED_ASSETS
        if not (assets_dir / name).exists()
    ]
    day_pages = sorted((output_root / "days").glob("*.html"))
    month_pages = sorted((output_root / "months").glob("*.html"))
    strategy_pages = sorted((output_root / "strategies").glob("*.html"))
    broken_links = _broken_links(
        output_root=output_root, repo_root=repo_root, html_files=html_files
    )
    script_hits = _script_hits(texts)
    js_hits = _unsafe_js_hits(assets_dir / "x2_interactions.js")
    secret_hits = _secret_hits(texts, assets_dir)
    external_hits = _external_hits(texts, assets_dir)
    action_hits = _action_control_hits(texts, assets_dir)
    validated_hits = _invalid_validated_hits(texts)
    absolute_hits = _absolute_path_hits(texts)
    missing_banner = [
        path for path, text in texts.items() if "Research-only / paper-only" not in text
    ]
    missing_live = [path for path, text in texts.items() if "Live trading disabled" not in text]
    missing_warning_panel = [path for path, text in texts.items() if "warnings-panel" not in text]
    shadow_official_hits = _shadow_official_hits(texts)
    missing_hero = _missing_hero_hits(texts)
    warning_drawer_hits = _warning_drawer_hits(texts)
    dense_card_wall_hits = _dense_card_wall_hits(texts)
    missing_scan_tables = _missing_scan_tables(output_root, texts)
    calendar_default_issue = _calendar_default_issue(output_root, texts)
    calendar_audit = _read_json(output_root / "reports/calendar_audit.json", {})
    bridge_x2_old = repo_root / "data/v2_command_center/command_center_x2.html"
    bridge_x2_x = repo_root / "data/v2_command_center_x/command_center_x2.html"
    checks = {
        "required_pages_exist": not missing_pages,
        "assets_exist": not missing_assets,
        "day_pages_generated": bool(day_pages),
        "month_pages_generated": bool(month_pages),
        "strategy_pages_generated": bool(strategy_pages),
        "broken_links_clear": not broken_links,
        "approved_local_script_tags_only": not script_hits,
        "local_js_safe": not js_hits,
        "secret_values_clear": not secret_hits,
        "external_dependencies_clear": not external_hits,
        "live_action_controls_clear": not action_hits,
        "invalid_validated_badges_clear": not validated_hits,
        "absolute_path_leaks_clear": not absolute_hits,
        "research_banner_all_pages": not missing_banner,
        "live_disabled_all_pages": not missing_live,
        "warnings_visible_all_pages": not missing_warning_panel,
        "hero_present_all_pages": not missing_hero,
        "warning_drawers_collapsed": not warning_drawer_hits,
        "dense_card_walls_clear": not dense_card_wall_hits,
        "scan_pages_use_tables": not missing_scan_tables,
        "calendar_defaults_to_evidence_month": not calendar_default_issue,
        "shadow_not_official": not shadow_official_hits,
        "calendar_audit_passed": calendar_audit.get("status") == "passed",
        "existing_command_center_bridge_exists": bridge_x2_old.exists(),
        "command_center_x_bridge_exists": bridge_x2_x.exists(),
    }
    detail = {
        "missing_pages": missing_pages,
        "missing_assets": missing_assets,
        "broken_links": broken_links,
        "script_hits": script_hits,
        "js_hits": js_hits,
        "secret_hits": secret_hits,
        "external_hits": external_hits,
        "action_hits": action_hits,
        "invalid_validated_hits": validated_hits,
        "absolute_path_hits": absolute_hits,
        "missing_banner": missing_banner,
        "missing_live_disabled": missing_live,
        "missing_warning_panel": missing_warning_panel,
        "missing_hero": missing_hero,
        "warning_drawer_hits": warning_drawer_hits,
        "dense_card_wall_hits": dense_card_wall_hits,
        "missing_scan_tables": missing_scan_tables,
        "calendar_default_issue": calendar_default_issue,
        "shadow_official_hits": shadow_official_hits,
        "day_pages": [path.as_posix() for path in day_pages],
        "month_pages": [path.as_posix() for path in month_pages],
        "strategy_pages": [path.as_posix() for path in strategy_pages],
    }
    warnings = [f"{key} failed" for key, passed in checks.items() if not passed]
    payload = {
        "schema_version": "v2.command_center_x2.qa.v1",
        "status": "passed" if not warnings else "failed",
        "page_count": len(html_files),
        "required_page_count": len(REQUIRED_PAGE_NAMES),
        "day_page_count": len(day_pages),
        "month_page_count": len(month_pages),
        "strategy_page_count": len(strategy_pages),
        "checks": checks,
        "detail": detail,
        "warnings": warnings,
    }
    qa_dir = output_root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    _write_json(qa_dir / "qa_latest.json", payload)
    (qa_dir / "qa_latest.md").write_text(_qa_md(payload), encoding="utf-8", newline="\n")
    return payload


def _html_files(output_root: Path) -> list[Path]:
    files = [output_root / "index.html"]
    for dirname in ("pages", "days", "months", "strategies"):
        files.extend(sorted((output_root / dirname).glob("*.html")))
    return [path for path in files if path.exists()]


def _broken_links(*, output_root: Path, repo_root: Path, html_files: list[Path]) -> list[str]:
    broken: list[str] = []
    allowed_roots = (repo_root.resolve(), output_root.resolve().parent)
    for path in html_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for href in _hrefs(text):
            if href.startswith("#"):
                continue
            if "://" in href or href.startswith("mailto:"):
                broken.append(f"{path.as_posix()}->{href}")
                continue
            target_href = _local_url_path(href)
            target = (path.parent / target_href).resolve()
            if not any(_is_relative_to(target, root) for root in allowed_roots):
                broken.append(f"{path.as_posix()}->{href}")
                continue
            if not target.exists():
                broken.append(f"{path.as_posix()}->{href}")
    return broken


def _script_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    parser = _TagParser()
    for path, text in texts.items():
        parser.reset_state()
        parser.feed(text)
        for src in parser.script_srcs:
            src_path = _local_url_path(src)
            if not src_path.endswith("assets/x2_interactions.js"):
                hits.append(f"{path}:{src}")
    return hits


def _unsafe_js_hits(path: Path) -> list[str]:
    if not path.exists():
        return [path.as_posix()]
    text = path.read_text(encoding="utf-8")
    forbidden = [
        "eval(",
        "Function(",
        "fetch(",
        "XMLHttpRequest",
        "sendBeacon",
        "analytics",
        "localStorage",
        "sessionStorage",
        "http://",
        "https://",
    ]
    return [f"{path.as_posix()}:{term}" for term in forbidden if term in text]


def _secret_hits(texts: dict[str, str], assets_dir: Path) -> list[str]:
    patterns = [
        r"sk-[A-Za-z0-9]{12,}",
        r"xox[baprs]-[A-Za-z0-9-]{12,}",
        r"TELEGRAM_BOT_TOKEN",
        r"TELEGRAM_CHAT_ID",
        r"BOT_TOKEN",
        r"CHAT_ID",
        r"API keys\.txt",
        r"Bearer\s+[A-Za-z0-9._-]{12,}",
    ]
    all_texts = dict(texts)
    for asset_path in assets_dir.glob("*"):
        if asset_path.is_file():
            all_texts[asset_path.as_posix()] = asset_path.read_text(encoding="utf-8")
    hits: list[str] = []
    for text_path, text in all_texts.items():
        for pattern in patterns:
            if re.search(pattern, text):
                hits.append(f"{text_path}:{pattern}")
    return hits


def _external_hits(texts: dict[str, str], assets_dir: Path) -> list[str]:
    all_texts = dict(texts)
    for asset_path in assets_dir.glob("*"):
        if asset_path.is_file():
            all_texts[asset_path.as_posix()] = asset_path.read_text(encoding="utf-8")
    hits: list[str] = []
    for text_path, text in all_texts.items():
        lower = text.lower()
        if "https://" in lower or "http://" in lower or "cdn." in lower:
            hits.append(text_path)
    return hits


def _action_control_hits(texts: dict[str, str], assets_dir: Path) -> list[str]:
    terms = [
        "buy button",
        "sell button",
        "place " + "order",
        "submit" + "_order",
        "create" + "_order",
        "execute" + "_trade",
        "live execution control",
        "real-money execution",
    ]
    all_texts = dict(texts)
    for asset_path in assets_dir.glob("*"):
        if asset_path.is_file():
            all_texts[asset_path.as_posix()] = asset_path.read_text(encoding="utf-8")
    hits: list[str] = []
    for text_path, text in all_texts.items():
        lower = text.lower()
        for term in terms:
            if term.lower() in lower:
                hits.append(f"{text_path}:{term}")
    return hits


def _invalid_validated_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        if 'data-trust="validated"' in text or ">Validated<" in text:
            hits.append(path)
    return hits


def _absolute_path_hits(texts: dict[str, str]) -> list[str]:
    pattern = re.compile(r"\b[A-Za-z]:[\\/](?![\\/])[^\"'<>\s]+")
    return [path for path, text in texts.items() if pattern.search(text)]


def _shadow_official_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        lower = text.lower()
        if "shadow" in lower and "official champion" in lower:
            hits.append(path)
    return hits


def _missing_hero_hits(texts: dict[str, str]) -> list[str]:
    return [path for path, text in texts.items() if "hero-story" not in text]


def _warning_drawer_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        if "details" not in text or "app-warnings-panel" not in text:
            hits.append(f"{path}:missing-details-warning-drawer")
        if re.search(r"<details[^>]*app-warnings-panel[^>]*\sopen(?:\s|=|>)", text):
            hits.append(f"{path}:warning-drawer-open")
    return hits


def _dense_card_wall_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        story_cards = text.count("story-card")
        if story_cards > 8:
            hits.append(f"{path}:story-card-count={story_cards}")
        lower = path.lower()
        if ("/days/" in lower or "\\days\\" in lower) and "strategy-card" in text:
            hits.append(f"{path}:day-strategy-card-wall")
        if lower.endswith("/pages/strategies.html") and "strategy-card" in text:
            hits.append(f"{path}:strategy-index-card-wall")
    return hits


def _missing_scan_tables(output_root: Path, texts: dict[str, str]) -> list[str]:
    required = [
        output_root / "pages/strategies.html",
        output_root / "pages/reports.html",
        output_root / "pages/evidence.html",
        output_root / "pages/automation.html",
        output_root / "pages/learning.html",
        output_root / "pages/market_masters.html",
        output_root / "pages/system_map.html",
    ]
    hits: list[str] = []
    for path in required:
        text = texts.get(path.as_posix(), "")
        if "<table" not in text:
            hits.append(path.as_posix())
    day_hits = [
        path
        for path, text in texts.items()
        if ("/days/" in path or "\\days\\" in path) and "<table" not in text
    ]
    hits.extend(day_hits[:20])
    return hits


def _calendar_default_issue(output_root: Path, texts: dict[str, str]) -> list[str]:
    months = _read_json(output_root / "data/months.json", [])
    if not isinstance(months, list) or not months:
        return []
    evidence_month = _latest_evidence_month(months)
    if not evidence_month:
        return []
    expected = str(evidence_month.get("month", ""))
    calendar_path = (output_root / "pages/calendar.html").as_posix()
    calendar_text = texts.get(calendar_path, "")
    if expected and f"<h2>{expected}</h2>" not in calendar_text:
        return [f"{calendar_path}:expected-default-month={expected}"]
    return []


def _latest_evidence_month(months: list[Any]) -> dict[str, Any]:
    evidence_months = [
        month for month in months if isinstance(month, dict) and _month_has_evidence(month)
    ]
    return evidence_months[-1] if evidence_months else {}


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
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _local_url_path(value: str) -> str:
    return value.split("#", 1)[0].split("?", 1)[0]


def _qa_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Command Center X2 QA",
        "",
        f"- Status: `{payload['status']}`",
        f"- Pages: `{payload['page_count']}`",
        f"- Required pages: `{payload['required_page_count']}`",
        f"- Day pages: `{payload['day_page_count']}`",
        f"- Month pages: `{payload['month_page_count']}`",
        f"- Strategy pages: `{payload['strategy_page_count']}`",
        "",
        "## Checks",
        "",
    ]
    for key, value in sorted(payload.get("checks", {}).items()):
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings", [])
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


class _TagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.script_srcs: list[str] = []

    def reset_state(self) -> None:
        self.hrefs = []
        self.script_srcs = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag in {"a", "link"} and attr.get("href"):
            self.hrefs.append(str(attr["href"]))
        if tag == "script" and attr.get("src"):
            self.script_srcs.append(str(attr["src"]))


def _hrefs(text: str) -> list[str]:
    parser = _TagParser()
    parser.feed(text)
    return parser.hrefs
