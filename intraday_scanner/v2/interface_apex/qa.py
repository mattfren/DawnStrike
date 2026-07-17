"""Interface Apex static UI QA."""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REQUIRED_PAGE_NAMES = (
    "mission.html",
    "calendar.html",
    "strategies.html",
    "trades.html",
    "intelligence.html",
    "system.html",
    "no_picks.html",
)

REQUIRED_ASSETS = (
    "apex.css",
    "apex_components.css",
    "apex.js",
    "apex_tokens.json",
)


def run_interface_apex_qa(
    *,
    output_root: Path = Path("data/v2_interface_apex"),
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    pages_dir = output_root / "pages"
    assets_dir = output_root / "assets"
    html_files = _html_files(output_root)
    texts = {
        path.as_posix(): path.read_text(encoding="utf-8") for path in html_files if path.exists()
    }
    asset_texts = _asset_texts(assets_dir)
    all_texts = {**texts, **asset_texts}
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
    trade_pages = sorted((output_root / "trades").glob("*.html"))
    broken_links = _broken_links(output_root=output_root, repo_root=repo_root, html_files=html_files)
    secret_hits = _secret_hits(all_texts)
    action_hits = _action_control_hits(texts)
    external_hits = _external_hits(all_texts)
    invalid_validated_hits = _invalid_validated_hits(texts)
    table_first_hits = _table_first_hits(texts)
    primary_raw_hits = _raw_table_primary_hits(texts)
    fetch_hits = _fetch_hits(all_texts)
    telegram_send_hits = _telegram_send_hits(all_texts)
    provider_call_hits = _provider_call_hits(all_texts)
    mission_text = texts.get((pages_dir / "mission.html").as_posix(), "")
    calendar_text = texts.get((pages_dir / "calendar.html").as_posix(), "")
    strategies_text = texts.get((pages_dir / "strategies.html").as_posix(), "")
    trades_text = texts.get((pages_dir / "trades.html").as_posix(), "")
    no_picks_text = texts.get((pages_dir / "no_picks.html").as_posix(), "")
    intelligence_text = texts.get((pages_dir / "intelligence.html").as_posix(), "")
    system_text = texts.get((pages_dir / "system.html").as_posix(), "")
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
        "mission_page_has_plain_english_headline": "Dawnstrike is running" in mission_text,
        "calendar_has_clickable_days": 'href="../days/' in calendar_text,
        "day_pages_exist": bool(day_pages),
        "strategy_cards_exist": "strategy-card" in strategies_text,
        "trade_cards_exist": "trade-card" in trades_text,
        "no_picks_reasons_exist": "That can be a disciplined result" in no_picks_text
        and "no-picks-reason" in no_picks_text,
        "intelligence_page_exists": "What Dawnstrike learned" in intelligence_text
        and "Research-inspired ideas" in intelligence_text,
        "system_contains_technical_diagnostics": "Advanced diagnostics" in system_text
        and "FillTruth" in system_text
        and "CommitBridge" in system_text,
        "raw_data_secondary": not primary_raw_hits,
        "warnings_visible": not missing_warning_visibility,
        "research_only_banner_visible": not missing_research,
        "live_trading_disabled_visible": not missing_live_disabled,
        "no_buy_sell_buttons": not action_hits,
        "no_live_controls": not action_hits,
        "no_invalid_validated_badges": not invalid_validated_hits,
        "no_shadow_as_official": _shadow_not_official(texts),
        "no_swing_as_day_trade": "Daily-bar swing research" in strategies_text
        and "swing research is a day trade" not in strategies_text.lower(),
        "no_secrets": not secret_hits,
        "no_external_cdn": not external_hits,
        "links_resolve": not broken_links,
        "local_js_only": _local_js_only(texts) and not external_hits,
        "no_external_fetch_calls": not fetch_hits,
        "no_provider_calls": not provider_call_hits,
        "no_telegram_sends": not telegram_send_hits,
        "prior_uis_preserved": all(
            (repo_root / path).exists()
            for path in (
                "data/v2_command_center",
                "data/v2_command_center_x",
                "data/v2_command_center_x2",
                "data/v2_command_center_x3",
            )
        ),
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
        "fetch_hits": fetch_hits,
        "provider_call_hits": provider_call_hits,
        "telegram_send_hits": telegram_send_hits,
        "missing_research": missing_research,
        "missing_live_disabled": missing_live_disabled,
        "missing_warning_visibility": missing_warning_visibility,
        "day_pages": [path.as_posix() for path in day_pages],
        "month_pages": [path.as_posix() for path in month_pages],
        "strategy_pages": [path.as_posix() for path in strategy_pages],
        "trade_pages": [path.as_posix() for path in trade_pages],
    }
    warnings = [f"{key} failed" for key, passed in checks.items() if not passed]
    payload = {
        "schema_version": "v2.interface_apex.qa.v1",
        "status": "passed" if not warnings else "failed",
        "page_count": len(html_files),
        "required_page_count": len(REQUIRED_PAGE_NAMES),
        "top_level_nav_count": max(nav_counts.values()) if nav_counts else 0,
        "day_page_count": len(day_pages),
        "month_page_count": len(month_pages),
        "strategy_page_count": len(strategy_pages),
        "trade_page_count": len(trade_pages),
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
    for dirname in ("pages", "days", "months", "strategies", "trades"):
        files.extend(sorted((output_root / dirname).glob("*.html")))
    return [path for path in files if path.exists()]


def _asset_texts(assets_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in assets_dir.glob("*"):
        if path.is_file() and path.suffix in {".css", ".js", ".json", ".svg"}:
            result[path.as_posix()] = path.read_text(encoding="utf-8")
    return result


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
        first_story = min(
            [idx for idx in [body.find("<h1"), body.find("mission-hero"), body.find("story-card")] if idx >= 0]
            or [999999]
        )
        if first_table >= 0 and first_table < first_story:
            hits.append(path)
    return hits


def _raw_table_primary_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        table_index = text.find("<table")
        raw_index = min(
            [idx for idx in [text.find("raw-data"), text.find("Advanced: source data and artifacts")] if idx >= 0]
            or [999999]
        )
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
            target_text = href.split("#", 1)[0].split("?", 1)[0]
            if not target_text:
                continue
            target = (path.parent / target_text).resolve()
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
        if tag not in {"a", "link", "script"}:
            return
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.hrefs.append(value)


def _secret_hits(texts: dict[str, str]) -> list[str]:
    terms = (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ALPACA_SECRET_KEY",
        "ALPACA_API_SECRET",
        "ALPACA_API_SECRET_KEY",
        "TWELVE_DATA_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "DAWNSTRIKE_ADMIN_TOKEN",
        "CRON_SECRET",
    )
    hits: list[str] = []
    for path, text in texts.items():
        for term in terms:
            if term in text:
                hits.append(f"{path}:{term}")
    return hits


def _action_control_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    button_pattern = re.compile(r"<(?:button|a)[^>]*>(.*?)</(?:button|a)>", re.S | re.I)
    forbidden = (
        "buy",
        "sell",
        "place order",
        "broker order",
        "real-money execution",
        "live execution control",
        "order-entry",
    )
    for path, text in texts.items():
        for match in button_pattern.finditer(text):
            label = re.sub(r"<[^>]+>", " ", match.group(1)).lower()
            if any(term in label for term in forbidden):
                hits.append(f"{path}:{label.strip()[:80]}")
    return hits


def _external_hits(texts: dict[str, str]) -> list[str]:
    terms = ("https://", "http://", "fonts.googleapis", "googletagmanager", "analytics", "cdn.")
    hits: list[str] = []
    for path, text in texts.items():
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


def _fetch_hits(texts: dict[str, str]) -> list[str]:
    terms = ("fetch(", "xmlhttprequest", "navigator.sendbeacon")
    return _term_hits(texts, terms)


def _provider_call_hits(texts: dict[str, str]) -> list[str]:
    terms = ("api.alpaca", "alphavantage", "twelvedata", "provider.fetch", "run_provider")
    return _term_hits(texts, terms)


def _telegram_send_hits(texts: dict[str, str]) -> list[str]:
    terms = ("api.telegram.org", "sendmessage", "send_telegram", "telegram_send(")
    return _term_hits(texts, terms)


def _term_hits(texts: dict[str, str], terms: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        lower = text.lower()
        for term in terms:
            if term in lower:
                hits.append(f"{path}:{term}")
    return hits


def _local_js_only(texts: dict[str, str]) -> bool:
    for text in texts.values():
        for src in re.findall(r"<script[^>]+src=\"([^\"]+)\"", text):
            if src.startswith(("http://", "https://", "//")):
                return False
            if "apex.js" not in src:
                return False
    return True


def _shadow_not_official(texts: dict[str, str]) -> bool:
    bad_patterns = (
        "shadow official",
        "official shadow",
        "shadow challenger is official",
        "shadow strategy is official",
        "shadow shown as official",
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
    lines = [
        "# Interface Apex QA",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Top-level nav count: `{payload.get('top_level_nav_count')}`",
        "",
        "| Check | Status |",
        "|---|---|",
    ]
    for key, passed in checks.items():
        lines.append(f"| `{key}` | {'passed' if passed else 'failed'} |")
    lines.append("")
    return "\n".join(lines)
