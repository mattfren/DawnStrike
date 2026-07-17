"""Command Center X static UI QA."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REQUIRED_PAGE_NAMES = (
    "today.html",
    "evidence.html",
    "paper_trading.html",
    "strategies.html",
    "learning.html",
    "market_masters.html",
    "risk.html",
    "automation.html",
    "reports.html",
    "system_map.html",
    "system.html",
    "repo_inventory.html",
    "data_flow.html",
    "cli_map.html",
    "artifact_map.html",
    "docs_map.html",
    "tests_map.html",
    "warnings.html",
    "no_picks.html",
    "telegram.html",
    "scheduler.html",
    "watchdog.html",
)

VIEW_MODEL_NAMES = (
    "system_health",
    "today",
    "evidence",
    "paper_trading",
    "strategies",
    "learning",
    "market_masters",
    "automation",
    "repo_inventory",
)


def run_command_center_x_qa(
    *,
    output_root: Path = Path("data/v2_command_center_x"),
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    pages_dir = output_root / "pages"
    assets_dir = output_root / "assets"
    data_dir = output_root / "data"
    html_files = [output_root / "index.html"] + sorted(pages_dir.glob("*.html"))
    required_pages = [pages_dir / name for name in REQUIRED_PAGE_NAMES]
    missing_pages = [path.as_posix() for path in required_pages if not path.exists()]
    missing_assets = [
        path.as_posix()
        for path in (
            assets_dir / "command_center_x.css",
            assets_dir / "design_tokens.json",
        )
        if not path.exists()
    ]
    missing_view_models = [
        (data_dir / f"{name}.json").as_posix()
        for name in VIEW_MODEL_NAMES
        if not (data_dir / f"{name}.json").exists()
    ]
    broken_links = _broken_links(output_root=output_root, html_files=html_files)
    texts = {
        path.as_posix(): path.read_text(encoding="utf-8") for path in html_files if path.exists()
    }
    secret_hits = _secret_hits(texts)
    external_hits = _external_hits(texts)
    script_hits = [path for path, text in texts.items() if "<script" in text.lower()]
    missing_banner = [
        path for path, text in texts.items() if "Research-only / paper-only" not in text
    ]
    missing_disabled = [path for path, text in texts.items() if "Live trading disabled" not in text]
    action_control_hits = _action_control_hits(texts)
    invalid_validated_hits = _invalid_validated_hits(texts)
    absolute_path_hits = _absolute_path_hits(texts)
    bridge_path = repo_root / "data/v2_command_center/command_center_x.html"
    warnings = []
    checks = {
        "required_pages_exist": not missing_pages,
        "assets_exist": not missing_assets,
        "view_models_exist": not missing_view_models,
        "broken_links_clear": not broken_links,
        "secret_values_clear": not secret_hits,
        "external_dependencies_clear": not external_hits,
        "script_tags_clear": not script_hits,
        "research_banner_all_pages": not missing_banner,
        "live_disabled_all_pages": not missing_disabled,
        "live_action_controls_clear": not action_control_hits,
        "invalid_validated_badges_clear": not invalid_validated_hits,
        "absolute_path_leaks_clear": not absolute_path_hits,
        "existing_command_center_bridge_exists": bridge_path.exists(),
    }
    detail = {
        "missing_pages": missing_pages,
        "missing_assets": missing_assets,
        "missing_view_models": missing_view_models,
        "broken_links": broken_links,
        "secret_hits": secret_hits,
        "external_hits": external_hits,
        "script_hits": script_hits,
        "missing_banner": missing_banner,
        "missing_live_disabled": missing_disabled,
        "action_control_hits": action_control_hits,
        "invalid_validated_hits": invalid_validated_hits,
        "absolute_path_hits": absolute_path_hits,
    }
    for key, passed in checks.items():
        if not passed:
            warnings.append(f"{key} failed")
    payload: dict[str, Any] = {
        "schema_version": "v2.command_center_x.qa.v1",
        "status": "passed" if not warnings else "failed",
        "page_count": len(html_files),
        "required_page_count": len(REQUIRED_PAGE_NAMES),
        "checks": checks,
        "detail": detail,
        "warnings": warnings,
    }
    qa_dir = output_root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    _write_json(qa_dir / "qa_latest.json", payload)
    (qa_dir / "qa_latest.md").write_text(_qa_md(payload), encoding="utf-8", newline="\n")
    return payload


def _broken_links(*, output_root: Path, html_files: list[Path]) -> list[str]:
    broken: list[str] = []
    for path in html_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for href in _hrefs(text):
            if href.startswith("#"):
                continue
            # Hardened archive pages link explicitly to the canonical runtime
            # dashboard at the application root.  That route is resolved by
            # the serving app, not by the static artifact directory.
            if href == "/":
                continue
            if "://" in href or href.startswith("mailto:"):
                broken.append(f"{path.as_posix()}->{href}")
                continue
            target = (path.parent / href).resolve()
            try:
                target.relative_to(output_root.resolve().parent)
            except ValueError:
                broken.append(f"{path.as_posix()}->{href}")
                continue
            if not target.exists():
                broken.append(f"{path.as_posix()}->{href}")
    return broken


def _secret_hits(texts: dict[str, str]) -> list[str]:
    patterns = [
        r"sk-[A-Za-z0-9]{12,}",
        r"xox[baprs]-[A-Za-z0-9-]{12,}",
        r"TELEGRAM_BOT_TOKEN",
        r"TELEGRAM_CHAT_ID",
        r"BOT_TOKEN",
        r"CHAT_ID",
        r"API keys\.txt",
    ]
    hits: list[str] = []
    for path, text in texts.items():
        for pattern in patterns:
            if re.search(pattern, text):
                hits.append(f"{path}:{pattern}")
    return hits


def _external_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        if "https://" in text or "http://" in text or "cdn." in text.lower():
            hits.append(path)
    return hits


def _action_control_hits(texts: dict[str, str]) -> list[str]:
    terms = [
        "buy button",
        "sell button",
        "place " + "order",
        "submit" + "_order",
        "create" + "_order",
        "execute" + "_trade",
        "live execution control",
    ]
    hits: list[str] = []
    for path, text in texts.items():
        lower = text.lower()
        for term in terms:
            if term.lower() in lower:
                hits.append(f"{path}:{term}")
    return hits


def _invalid_validated_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for path, text in texts.items():
        if 'data-trust="validated"' in text or ">Validated<" in text:
            hits.append(path)
    return hits


def _absolute_path_hits(texts: dict[str, str]) -> list[str]:
    hits: list[str] = []
    pattern = re.compile(r"\b[A-Za-z]:[\\/](?![\\/])[^\"'<>\s]+")
    for path, text in texts.items():
        if pattern.search(text):
            hits.append(path)
    return hits


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _qa_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Command Center X QA",
        "",
        f"- Status: `{payload['status']}`",
        f"- Pages: `{payload['page_count']}`",
        f"- Required pages: `{payload['required_page_count']}`",
        "",
        "## Checks",
        "",
    ]
    checks = payload.get("checks", {})
    if isinstance(checks, dict):
        for key, value in sorted(checks.items()):
            lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Warnings", ""])
    warnings = payload.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"a", "link"}:
            return
        attr_name = "href"
        for key, value in attrs:
            if key == attr_name and value:
                self.hrefs.append(value)


def _hrefs(text: str) -> list[str]:
    parser = _HrefParser()
    parser.feed(text)
    return parser.hrefs
