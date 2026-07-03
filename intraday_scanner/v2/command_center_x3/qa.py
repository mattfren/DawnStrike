"""Command Center X3 static UI QA."""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REQUIRED_PAGE_NAMES = (
    "home.html",
    "calendar.html",
    "strategies.html",
    "trades.html",
    "no_picks.html",
    "system.html",
)

REQUIRED_ASSETS = ("x3.css", "x3.js", "x3_tokens.json", "x3_favicon.svg")


def run_command_center_x3_qa(
    *,
    output_root: Path = Path("data/v2_command_center_x3"),
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
    nav_counts = {path: _primary_nav_count(text) for path, text in texts.items()}
    day_pages = sorted((output_root / "days").glob("*.html"))
    month_pages = sorted((output_root / "months").glob("*.html"))
    strategy_pages = sorted((output_root / "strategies").glob("*.html"))
    broken_links = _broken_links(output_root=output_root, repo_root=repo_root, html_files=html_files)
    secret_hits = _secret_hits(texts, assets_dir)
    action_hits = _action_control_hits(texts, assets_dir)
    external_hits = _external_hits(texts, assets_dir)
    invalid_validated_hits = _invalid_validated_hits(texts)
    table_first_hits = _table_first_hits(texts)
    primary_raw_hits = _raw_table_primary_hits(texts)
    missing_research = [
        path for path, text in texts.items() if "Research-only" not in text
    ]
    missing_live_disabled = [
        path for path, text in texts.items() if "Live trading disabled" not in text
    ]
    missing_warning_visibility = [
        path for path, text in texts.items() if "Warnings stay visible" not in text
    ]
    checks = {
        "required_pages_exist": not missing_pages,
        "required_assets_exist": not missing_assets,
        "top_level_nav_count_ok": nav_counts and all(count <= 6 for count in nav_counts.values()),
        "no_page_starts_with_table": not table_first_hits,
        "home_has_story_summary": "story-summary" in texts.get((pages_dir / "home.html").as_posix(), ""),
        "calendar_exists": (pages_dir / "calendar.html").exists() and bool(month_pages),
        "calendar_has_day_links": 'href="../days/' in texts.get((pages_dir / "calendar.html").as_posix(), ""),
        "day_pages_exist": bool(day_pages),
        "strategy_cards_exist": "strategy-card" in texts.get((pages_dir / "strategies.html").as_posix(), ""),
        "trade_cards_exist": "trade-card" in texts.get((pages_dir / "trades.html").as_posix(), ""),
        "no_picks_reasons_visible": "Why Dawnstrike waited" in texts.get((pages_dir / "no_picks.html").as_posix(), ""),
        "system_page_contains_technical_details": "Advanced artifact links" in texts.get((pages_dir / "system.html").as_posix(), "") and "FillTruth" in texts.get((pages_dir / "system.html").as_posix(), ""),
        "raw_tables_not_primary": not primary_raw_hits,
        "no_secrets": not secret_hits,
        "no_live_trading_controls": not action_hits,
        "no_invalid_validated_badge": not invalid_validated_hits,
        "warnings_visible": not missing_warning_visibility,
        "no_external_cdn": not external_hits,
        "links_resolve": not broken_links,
        "research_banner_present": not missing_research,
        "live_trading_disabled_visible": not missing_live_disabled,
        "responsive_meta_present": all('name="viewport"' in text for text in texts.values()),
        "shadow_not_official": _shadow_not_official(texts),
        "swing_not_day_trade": "Swing research, separated" in texts.get((pages_dir / "strategies.html").as_posix(), ""),
        "x2_preserved": (repo_root / "data/v2_command_center_x2/index.html").exists(),
    }
    detail = {
        "missing_pages": missing_pages,
        "missing_assets": missing_assets,
        "nav_counts": nav_counts,
        "table_first_hits": table_first_hits,
        "primary_raw_hits": primary_raw_hits,
        "broken_links": broken_links,
        "secret_hits": secret_hits,
        "action_hits": action_hits,
        "external_hits": external_hits,
        "invalid_validated_hits": invalid_validated_hits,
        "missing_research": missing_research,
        "missing_live_disabled": missing_live_disabled,
        "missing_warning_visibility": missing_warning_visibility,
        "day_pages": [path.as_posix() for path in day_pages],
        "month_pages": [path.as_posix() for path in month_pages],
        "strategy_pages": [path.as_posix() for path in strategy_pages],
    }
    warnings = [f"{key} failed" for key, passed in checks.items() if not passed]
    payload = {
        "schema_version": "v2.command_center_x3.qa.v1",
        "status": "passed" if not warnings else "failed",
        "page_count": len(html_files),
        "required_page_count": len(REQUIRED_PAGE_NAMES),
        "top_level_nav_count": max(nav_counts.values()) if nav_counts else 0,
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


def _primary_nav_count(text: str) -> int:
    match = re.search(r"<nav[^>]*data-primary-nav[^>]*>(.*?)</nav>", text, re.S)
    if not match:
        return 999
    return len(re.findall(r"<a(?:\s|>)", match.group(1)))


def _table_first_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        main_index = text.find("<main")
        body = text[main_index:] if main_index >= 0 else text
        first_table = body.find("<table")
        first_headline = min([idx for idx in [body.find("<h1"), body.find("story-summary")] if idx >= 0] or [999999])
        if first_table >= 0 and first_table < first_headline:
            hits.append(path)
    return hits


def _raw_table_primary_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        table_index = text.find("<table")
        raw_index = min([idx for idx in [text.find("raw-drawer"), text.find("Advanced artifact links")] if idx >= 0] or [999999])
        if table_index >= 0 and table_index < raw_index:
            hits.append(path)
    return hits


def _broken_links(*, output_root: Path, repo_root: Path, html_files: list[Path]) -> list[str]:
    broken: list[str] = []
    allowed_roots = (repo_root.resolve(), output_root.resolve().parent)
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        for href in _hrefs(text):
            if href.startswith("#"):
                continue
            if "://" in href or href.startswith("mailto:"):
                broken.append(f"{path.as_posix()}->{href}")
                continue
            target = (path.parent / href.split("#", 1)[0]).resolve()
            if not any(_is_relative_to(target, root) for root in allowed_roots):
                broken.append(f"{path.as_posix()}->{href}")
                continue
            if not target.exists():
                broken.append(f"{path.as_posix()}->{href}")
    return broken


def _hrefs(text: str) -> list[str]:
    parser = _HrefParser()
    parser.feed(text)
    return parser.hrefs


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _secret_hits(texts: dict[str, str], assets_dir: Path) -> list[str]:
    terms = (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ALPACA_SECRET_KEY",
        "ALPACA_API_SECRET",
        "TWELVE_DATA_API_KEY",
    )
    hits: list[str] = []
    all_texts = dict(texts)
    for asset in assets_dir.glob("*"):
        if asset.is_file():
            all_texts[asset.as_posix()] = asset.read_text(encoding="utf-8")
    for path, text in all_texts.items():
        for term in terms:
            if term in text:
                hits.append(f"{path}:{term}")
    return hits


def _action_control_hits(texts: dict[str, str], assets_dir: Path) -> list[str]:
    terms = (
        "buy button",
        "sell button",
        "place order",
        "submit" + "_order",
        "create" + "_order",
        "order-entry",
        "real-money execution",
        "live execution control",
        "broker order",
    )
    hits: list[str] = []
    all_texts = dict(texts)
    for asset in assets_dir.glob("*"):
        if asset.is_file():
            all_texts[asset.as_posix()] = asset.read_text(encoding="utf-8")
    for path, text in all_texts.items():
        lower = text.lower()
        for term in terms:
            if term in lower:
                hits.append(f"{path}:{term}")
    return hits


def _external_hits(texts: dict[str, str], assets_dir: Path) -> list[str]:
    terms = ("https://", "http://", "fonts.googleapis", "googletagmanager", "analytics", "cdn.")
    hits: list[str] = []
    all_texts = dict(texts)
    for asset in assets_dir.glob("*"):
        if asset.is_file():
            all_texts[asset.as_posix()] = asset.read_text(encoding="utf-8")
    for path, text in all_texts.items():
        lower = text.lower()
        for term in terms:
            if term in lower:
                hits.append(f"{path}:{term}")
    return hits


def _invalid_validated_hits(texts: dict[str, str]) -> list[str]:
    return [
        path
        for path, text in texts.items()
        if 'data-trust="validated"' in text or ">Validated<" in text
    ]


def _shadow_not_official(texts: dict[str, str]) -> bool:
    bad_patterns = (
        "shadow official",
        "official shadow",
        "shadow shown as official",
        "shadow strategy is official",
        "shadow challenger is official",
    )
    for text in texts.values():
        lower = text.lower()
        if any(pattern in lower for pattern in bad_patterns):
            return False
    return True


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _qa_md(payload: dict[str, Any]) -> str:
    checks = payload.get("checks", {})
    lines = ["# Command Center X3 QA", "", f"- Status: `{payload.get('status')}`", f"- Top-level nav count: `{payload.get('top_level_nav_count')}`", "", "| Check | Status |", "|---|---|"]
    for key, passed in checks.items():
        lines.append(f"| `{key}` | {'passed' if passed else 'failed'} |")
    lines.append("")
    return "\n".join(lines)
