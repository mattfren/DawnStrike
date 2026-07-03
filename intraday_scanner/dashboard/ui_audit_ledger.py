"""Route inventory ledger for the Dawnstrike dashboard audit."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

AUDIT_BUNDLE_PREFIXES = (
    "data/ui_ux_audit_visuals_",
    "data/ui_ux_audit_visuals_smoke_",
    "data/ui_ux_audit_webpages_",
)
APEX_PRO_HARDENED_ATTR = 'data-apex-pro-hardened="true"'
APEX_PRO_SHELL_MARKER = "dawnstrike-apex-pro-audit-shell:start"

IGNORED_DISCOVERY_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

DEV_ROUTE_PREFIXES = (
    ".pytest",
    "logs/",
    "outputs/",
    "tests/",
)

GENERIC_TITLES = {
    "",
    "untitled",
    "dawnstrike apex - trade paper basket",
    "dawnstrike - trade paper basket",
    "trade paper basket",
}

DATE_RE = re.compile(r"(?P<date>20\d{2}-\d{2}-\d{2})")
MONTH_RE = re.compile(r"(?P<month>20\d{2}-\d{2})(?:\.html)?$")
TRADE_RE = re.compile(
    r"paper-(?P<date>20\d{2}-\d{2}-\d{2})-(?P<strategy>.+)-paper-basket-(?P<basket>\d+)\.html$"
)


@dataclass(frozen=True)
class AuditLedgerRow:
    index: int
    source: str
    family: str
    route_boundary: str
    target_template: str
    derived_title: str
    derived_h1: str
    action_taken: str
    visual_status: str
    screenshot_evidence: str
    remaining_exceptions: str
    group: str = ""
    manifest_title: str = ""
    manifest_h1: str = ""
    rendered_title: str = ""
    source_present: bool = False
    apex_pro_hardened: bool = False
    ledger_origin: str = "manifest"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_route(path: str | Path) -> str:
    return str(path).replace("\\", "/").strip("/")


def route_group(source: str) -> str:
    source = normalize_route(source)
    if source.startswith(".pytest"):
        return source.split("/", 1)[0]
    parts = source.split("/")
    if len(parts) >= 2 and parts[0] == "data":
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def is_dev_route(source: str) -> bool:
    source = normalize_route(source)
    return source.startswith(DEV_ROUTE_PREFIXES) or any(
        token in source for token in ("/raw_source.html", "/rendered_source.html")
    )


def humanize_slug(value: str) -> str:
    cleaned = re.sub(r"\.html$", "", value)
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "Route"
    words = []
    for word in cleaned.split(" "):
        if word.upper() in {"ATR", "FVG", "OOS", "SMA", "VWAP"}:
            words.append(word.upper())
        elif word.lower() in {"orb", "ts", "mm"}:
            words.append(word.upper())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def route_family(source: str) -> str:
    source = normalize_route(source)
    if is_dev_route(source):
        return "raw_fixture_or_provider_capture"
    if "/days/" in source and DATE_RE.search(source):
        return "daily_session_detail"
    if "/months/" in source and MONTH_RE.search(source):
        return "monthly_calendar"
    if "/strategies/" in source:
        return "strategy_detail"
    if "/trades/" in source or "paper-basket" in source:
        return "paper_trade_basket_detail"
    if "historical_backtests" in source:
        return "historical_backtest_report"
    if "paper_ops" in source:
        return "paper_ops_calendar"
    if source.endswith("/index.html") or source == "index.html":
        return "application_root_page"
    if "/pages/" in source:
        return "application_subpage"
    return "application_subpage"


def route_boundary(source: str) -> str:
    group = route_group(source)
    if is_dev_route(source):
        return "dev-audit-quarantine"
    if group == "data/v2_interface_apex":
        return "apex-static-archive"
    if group.startswith("data/v2_command_center"):
        return "legacy-command-center-archive"
    if group == "data/v2_historical_backtests":
        return "research-archive"
    if group == "data/v2_paper_ops":
        return "paper-ops-archive"
    return "repo-extra-review"


def target_template(source: str) -> str:
    family = route_family(source)
    boundary = route_boundary(source)
    if family == "raw_fixture_or_provider_capture":
        return "Dev provider capture template"
    if family == "daily_session_detail":
        return "Daily session detail template"
    if family == "monthly_calendar":
        return "Monthly calendar heatmap template"
    if family == "strategy_detail":
        return "Strategy tear sheet template"
    if family == "paper_trade_basket_detail":
        return "Paper trade ticket template"
    if family == "historical_backtest_report":
        return "Backtest report template"
    if family == "paper_ops_calendar":
        return "PaperOps calendar template"
    if boundary == "legacy-command-center-archive":
        return "Legacy command module archive template"
    if boundary == "apex-static-archive":
        return "Apex static archive shell template"
    return "Application shell subpage template"


def _base_product_label(source: str) -> str:
    group = route_group(source)
    labels = {
        "data/v2_interface_apex": "Dawnstrike Apex Pro Archive",
        "data/v2_command_center_x3": "Dawnstrike X3 Archive",
        "data/v2_command_center_x2": "Dawnstrike X2 Archive",
        "data/v2_command_center_x": "Dawnstrike X Archive",
        "data/v2_command_center": "Dawnstrike Command Center Archive",
        "data/v2_historical_backtests": "Dawnstrike Backtest Archive",
        "data/v2_paper_ops": "Dawnstrike PaperOps Archive",
    }
    if route_boundary(source) == "dev-audit-quarantine":
        return "Dawnstrike Dev Audit Quarantine"
    return labels.get(group, "Dawnstrike Route Audit")


def derive_route_metadata(
    source: str,
    existing_title: str = "",
    existing_h1: str = "",
) -> tuple[str, str]:
    source = normalize_route(source)
    family = route_family(source)
    product = _base_product_label(source)

    trade = TRADE_RE.search(source)
    if trade:
        strategy = humanize_slug(trade.group("strategy"))
        basket = int(trade.group("basket")) + 1
        date = trade.group("date")
        title = f"{product} - {date} {strategy} Paper Basket {basket}"
        h1 = f"{date} paper basket {basket}: {strategy}"
        return title, h1

    if family == "daily_session_detail":
        date = DATE_RE.search(source)
        day = date.group("date") if date else "daily session"
        return f"{product} - Daily Session {day}", f"Daily session {day}"

    if family == "monthly_calendar":
        month = MONTH_RE.search(source)
        month_value = month.group("month") if month else "month"
        return f"{product} - Calendar {month_value}", f"Monthly calendar {month_value}"

    if family == "strategy_detail":
        slug = Path(source).stem
        strategy = humanize_slug(slug)
        return f"{product} - Strategy {strategy}", f"Strategy tear sheet: {strategy}"

    if family == "raw_fixture_or_provider_capture":
        provider = _dev_provider_label(source)
        title = f"{product} - {provider} Capture"
        h1 = f"Dev-only provider capture: {provider}"
        return title, h1

    page_name = Path(source).stem
    if page_name == "index":
        page_label = "Index"
    else:
        page_label = humanize_slug(page_name)

    cleaned_title = existing_title.strip()
    cleaned_h1 = existing_h1.strip()
    if cleaned_title and cleaned_title.lower() not in GENERIC_TITLES:
        title = cleaned_title
    else:
        title = f"{product} - {page_label}"
    if cleaned_h1:
        h1 = cleaned_h1
    else:
        h1 = page_label
    return title, h1


def _dev_provider_label(source: str) -> str:
    normalized = normalize_route(source).lower()
    for provider in (
        "stockanalysis",
        "tradingview",
        "marketwatch",
        "investing",
        "fixture_public_table",
        "browser",
        "auto",
        "ingest",
    ):
        if provider in normalized:
            return humanize_slug(provider)
    return humanize_slug(Path(source).parent.name)


def action_taken(source: str, source_present: bool, apex_pro_hardened: bool = False) -> str:
    boundary = route_boundary(source)
    if not source_present:
        return (
            "Retained from visual manifest; current repo path is absent "
            "and not production navigable."
        )
    if apex_pro_hardened:
        if boundary == "dev-audit-quarantine":
            return (
                "Quarantined from production nav and stamped with the dev-only "
                "Apex Pro audit shell."
            )
        if boundary == "legacy-command-center-archive":
            return (
                "Archived from production nav and stamped with the Apex Pro "
                "audit shell."
            )
        if boundary == "apex-static-archive":
            return (
                "Stamped with Apex Pro audit shell and retained as static "
                "archive evidence outside production nav."
            )
        return "Stamped with Apex Pro audit shell; route remains outside production nav."
    if boundary == "dev-audit-quarantine":
        return "Quarantined from production nav; retained only for dev/provider audit evidence."
    if boundary == "legacy-command-center-archive":
        return (
            "Archived behind audit ledger; production launcher points to "
            "the single Streamlit dashboard."
        )
    if boundary == "apex-static-archive":
        return (
            "Mapped to shared Apex Pro template target; excluded from "
            "primary dashboard nav until regenerated."
        )
    if boundary == "research-archive":
        return (
            "Recorded as research archive; requires explicit freshness "
            "and backtest caveats before promotion."
        )
    if boundary == "paper-ops-archive":
        return "Recorded as PaperOps archive; requires paper-trading boundary before promotion."
    return "Discovered outside the manifest; queued for manual route-family review."


def visual_status(row: dict[str, Any]) -> str:
    status = str(row.get("status", "") or "not-rendered")
    mode = str(row.get("mode", "") or "")
    width = row.get("width", "")
    height = row.get("height", "")
    error = str(row.get("error", "") or "")
    if error:
        return f"{status}; {error}"
    if mode and width and height:
        return f"{status}; {mode}; {width}x{height}"
    return status


def remaining_exceptions(
    source: str,
    row: dict[str, Any],
    source_present: bool,
    apex_pro_hardened: bool = False,
) -> str:
    exceptions: list[str] = []
    title = str(row.get("title", "") or row.get("rendered_title", "")).strip()
    if title.lower() in GENERIC_TITLES and not apex_pro_hardened:
        exceptions.append("replace blank/generic title before promotion")
    if route_family(source) == "paper_trade_basket_detail":
        if not apex_pro_hardened:
            exceptions.append("paper-trade ticket needs explicit risk/freshness/provider rail")
    boundary = route_boundary(source)
    if boundary == "dev-audit-quarantine":
        if not apex_pro_hardened:
            exceptions.append("dev-only banner required if browsed directly")
    elif boundary.endswith("-archive") or boundary == "research-archive":
        exceptions.append(
            "hidden from production nav; promote only through canonical dashboard shell"
        )
    if not source_present:
        exceptions.append("source file absent in current repo snapshot")
    if str(row.get("status", "") or "") != "ok":
        exceptions.append("visual render is not ok")
    if apex_pro_hardened:
        exceptions.append("rerender desktop/tablet/mobile visual QA after hardening")
    return "; ".join(dict.fromkeys(exceptions)) or "none"


def discover_html_routes(repo_root: Path) -> set[str]:
    routes: set[str] = set()
    for path in repo_root.rglob("*.html"):
        relative = normalize_route(path.relative_to(repo_root))
        parts = set(relative.split("/"))
        if parts & IGNORED_DISCOVERY_PARTS:
            continue
        if relative.startswith(AUDIT_BUNDLE_PREFIXES):
            continue
        routes.add(relative)
    return routes


def _load_manifest_rows(manifest_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(payload.get("rows", []))


def build_ledger_rows(
    *,
    repo_root: Path,
    manifest_path: Path,
    include_discovered_extras: bool = True,
) -> list[AuditLedgerRow]:
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    manifest_rows = _load_manifest_rows(manifest_path)
    discovered = discover_html_routes(repo_root) if include_discovered_extras else set()
    rows_by_source = {normalize_route(row.get("source", "")): row for row in manifest_rows}
    manifest_sources = set(rows_by_source)

    ordered_sources = [normalize_route(row.get("source", "")) for row in manifest_rows]
    if include_discovered_extras:
        for source in sorted(discovered - set(ordered_sources)):
            ordered_sources.append(source)
            rows_by_source[source] = {
                "group": route_group(source),
                "source": source,
                "audit_path": "",
                "title": "",
                "h1": "",
                "status": "repo-discovered",
                "mode": "",
                "screenshot": "",
                "rendered_title": "",
            }

    ledger: list[AuditLedgerRow] = []
    for index, source in enumerate(ordered_sources, start=1):
        row = rows_by_source[source]
        source_path = repo_root / source
        source_present = source_path.exists()
        apex_pro_hardened = _source_is_apex_pro_hardened(source_path) if source_present else False
        title, h1 = derive_route_metadata(
            source,
            existing_title=str(row.get("title", "") or row.get("rendered_title", "")),
            existing_h1=str(row.get("h1", "") or ""),
        )
        origin = "manifest" if source in manifest_sources else "repo-extra"
        ledger.append(
            AuditLedgerRow(
                index=index,
                source=source,
                family=route_family(source),
                route_boundary=route_boundary(source),
                target_template=target_template(source),
                derived_title=title,
                derived_h1=h1,
                action_taken=action_taken(source, source_present, apex_pro_hardened),
                visual_status=visual_status(row),
                screenshot_evidence=str(row.get("screenshot", "") or ""),
                remaining_exceptions=remaining_exceptions(
                    source,
                    row,
                    source_present,
                    apex_pro_hardened,
                ),
                group=str(row.get("group", "") or route_group(source)),
                manifest_title=str(row.get("title", "") or ""),
                manifest_h1=str(row.get("h1", "") or ""),
                rendered_title=str(row.get("rendered_title", "") or ""),
                source_present=source_present,
                apex_pro_hardened=apex_pro_hardened,
                ledger_origin=origin,
            )
        )
    return ledger


def ledger_summary(rows: list[AuditLedgerRow]) -> dict[str, Any]:
    families: dict[str, int] = {}
    boundaries: dict[str, int] = {}
    for row in rows:
        families[row.family] = families.get(row.family, 0) + 1
        boundaries[row.route_boundary] = boundaries.get(row.route_boundary, 0) + 1
    return {
        "row_count": len(rows),
        "source_present_count": sum(1 for row in rows if row.source_present),
        "apex_pro_hardened_count": sum(1 for row in rows if row.apex_pro_hardened),
        "manifest_count": sum(1 for row in rows if row.ledger_origin == "manifest"),
        "repo_extra_count": sum(1 for row in rows if row.ledger_origin == "repo-extra"),
        "visual_ok_count": sum(1 for row in rows if row.visual_status.startswith("ok;")),
        "families": dict(sorted(families.items())),
        "boundaries": dict(sorted(boundaries.items())),
    }


def write_ledger_json(rows: list[AuditLedgerRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": ledger_summary(rows),
        "rows": [row.to_dict() for row in rows],
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_ledger_markdown(
    rows: list[AuditLedgerRow],
    output_path: Path,
    *,
    manifest_path: Path,
    json_path: Path,
) -> None:
    summary = ledger_summary(rows)
    lines = [
        "# Dawnstrike UI Audit Completion Ledger",
        "",
        (
            "This ledger reconciles the latest visual route inventory with the current "
            "one-dashboard production boundary. Static/generated routes are recorded "
            "here for audit continuity; the canonical production surface remains the "
            "Streamlit operator dashboard launched from `app.py`."
        ),
        "",
        "## Summary",
        "",
        f"- Manifest input: `{manifest_path.as_posix()}`",
        f"- Machine-readable ledger: `{json_path.as_posix()}`",
        f"- Ledger row count: {summary['row_count']}",
        f"- Manifest rows retained: {summary['manifest_count']}",
        f"- Current repo HTML extras discovered: {summary['repo_extra_count']}",
        f"- Current source files present: {summary['source_present_count']}",
        f"- Routes stamped with Apex Pro audit shell: {summary['apex_pro_hardened_count']}",
        f"- Browser visual OK rows from manifest: {summary['visual_ok_count']}",
        (
            "- Production boundary: one canonical Streamlit dashboard; static HTML "
            "generations are archived, research-only, or dev/audit-only until "
            "regenerated through shared templates."
        ),
    ]
    lines.extend(_render_proof_sections(output_path.parent))
    lines.extend(
        [
            "",
            "## Route-Family Counts",
            "",
        ]
    )
    lines.extend(
        [
        "| Family | Count |",
        "| --- | ---: |",
        ]
    )
    for family, count in summary["families"].items():
        lines.append(f"| {family} | {count} |")

    lines.extend(
        [
            "",
            "## Boundary Counts",
            "",
            "| Boundary | Count |",
            "| --- | ---: |",
        ]
    )
    for boundary, count in summary["boundaries"].items():
        lines.append(f"| {boundary} | {count} |")

    lines.extend(
        [
            "",
            "## Route Ledger",
            "",
            (
                "| # | Path | Family | Boundary | Target Template | Derived Title / H1 | "
                "Action Taken | Visual Status | Screenshot Evidence | Remaining Exceptions |"
            ),
            "| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.index),
                    _md_escape(row.source),
                    _md_escape(row.family),
                    _md_escape(row.route_boundary),
                    _md_escape(row.target_template),
                    _md_escape(f"{row.derived_title} / {row.derived_h1}"),
                    _md_escape(row.action_taken),
                    _md_escape(row.visual_status),
                    _md_escape(row.screenshot_evidence or "n/a"),
                    _md_escape(row.remaining_exceptions),
                ]
            )
            + " |"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md_escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _render_proof_sections(repo_root: Path) -> list[str]:
    proof_path = repo_root / "data/ui_audit_multiviewport_proof.json"
    if not proof_path.exists():
        return [""]
    try:
        payload = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [""]

    lines = [
        "",
        "## Hardened Visual Proof",
        "",
        "| Viewport | Manifest | Rows | OK | Errors | Fallbacks |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for proof in payload.get("proofs", []):
        label = str(proof.get("viewport", "")).capitalize()
        width = proof.get("width", "")
        height = proof.get("height", "")
        viewport = f"{label} {width}x{height}".strip()
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_escape(viewport),
                    f"`{_md_escape(str(proof.get('manifest', '')) )}`",
                    str(proof.get("row_count", "")),
                    str(proof.get("ok_count", "")),
                    str(proof.get("error_count", "")),
                    str(proof.get("fallback_count", "")),
                ]
            )
            + " |"
        )

    runtime = payload.get("runtime_quality") or {}
    if runtime:
        lines.extend(
            [
                "",
                "## Runtime Quality Proof",
                "",
                (
                    "| Report | Rows | OK | Errors | Console Errors | Page Errors | "
                    "Request Failures | Bad Responses | DOM Issues |"
                ),
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                (
                    f"| `{_md_escape(str(runtime.get('report', '')) )}` | "
                    f"{runtime.get('route_count', '')} | {runtime.get('ok_count', '')} | "
                    f"{runtime.get('error_count', '')} | "
                    f"{runtime.get('console_error_count', '')} | "
                    f"{runtime.get('page_error_count', '')} | "
                    f"{runtime.get('request_failure_count', '')} | "
                    f"{runtime.get('bad_response_count', '')} | "
                    f"{runtime.get('dom_issue_count', '')} |"
                ),
            ]
        )
    return lines


def _source_is_apex_pro_hardened(source_path: Path) -> bool:
    try:
        text = source_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return APEX_PRO_HARDENED_ATTR in text and APEX_PRO_SHELL_MARKER in text
