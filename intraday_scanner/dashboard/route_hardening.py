"""Apply the shared Apex Pro audit shell to static route inventory pages."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.dashboard.ui_audit_ledger import (
    AuditLedgerRow,
    build_ledger_rows,
)

SHELL_START = "<!-- dawnstrike-apex-pro-audit-shell:start -->"
SHELL_END = "<!-- dawnstrike-apex-pro-audit-shell:end -->"
FOOTER_START = "<!-- dawnstrike-apex-pro-audit-footer:start -->"
FOOTER_END = "<!-- dawnstrike-apex-pro-audit-footer:end -->"
TOKEN_STYLE_ID = "dawnstrike-apex-pro-route-tokens"
HARDENED_ATTR = 'data-apex-pro-hardened="true"'


@dataclass(frozen=True)
class HardenResult:
    source: str
    changed: bool
    title: str
    boundary: str
    family: str


def harden_routes(repo_root: Path, manifest_path: Path) -> list[HardenResult]:
    rows = build_ledger_rows(repo_root=repo_root, manifest_path=manifest_path)
    results: list[HardenResult] = []
    for row in rows:
        path = repo_root / row.source
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        hardened = harden_html(original, row)
        changed = hardened != original
        if changed:
            path.write_text(hardened, encoding="utf-8")
        results.append(
            HardenResult(
                source=row.source,
                changed=changed,
                title=row.derived_title,
                boundary=row.route_boundary,
                family=row.family,
            )
        )
    return results


def harden_html(source_html: str, row: AuditLedgerRow) -> str:
    doc = _ensure_document(source_html)
    doc = _upsert_document_basics(doc)
    doc = _strip_managed_blocks(doc)
    doc = _disable_legacy_runtime(doc, row)
    doc = _upsert_title(doc, row.derived_title)
    doc = _upsert_head_tokens(doc)
    doc = _upsert_body_attributes(doc, row)
    doc = _upsert_first_h1(doc, row.derived_h1)
    doc = _insert_after_body_open(doc, _shell_markup(row))
    doc = _insert_before_body_close(doc, _footer_markup(row))
    return _normalize_managed_spacing(doc)


def is_hardened_html(source_html: str) -> bool:
    return (
        HARDENED_ATTR in source_html
        and SHELL_START in source_html
        and FOOTER_START in source_html
    )


def _ensure_document(source_html: str) -> str:
    doc = source_html
    if not re.search(r"<html\b", doc, flags=re.I):
        doc = (
            '<!doctype html>\n<html lang="en">\n<head></head>\n<body>\n'
            + doc
            + "\n</body>\n</html>\n"
        )
    if not re.search(r"<head\b", doc, flags=re.I):
        doc = re.sub(r"(<html\b[^>]*>)", r"\1\n<head></head>", doc, count=1, flags=re.I)
    if not re.search(r"<body\b", doc, flags=re.I):
        doc = re.sub(r"(</head>)", r"\1\n<body>", doc, count=1, flags=re.I)
        doc = re.sub(r"(</html>)", r"</body>\n\1", doc, count=1, flags=re.I)
    return doc


def _upsert_document_basics(doc: str) -> str:
    doc = re.sub(
        r"<html\b([^>]*)>",
        _html_tag_with_lang,
        doc,
        count=1,
        flags=re.I,
    )
    if not re.search(r"<meta\b[^>]*charset=", doc, flags=re.I):
        doc = re.sub(
            r"(<head\b[^>]*>)",
            "\\1\n  <meta charset=\"utf-8\">",
            doc,
            count=1,
            flags=re.I,
        )
    if not re.search(r"<meta\b[^>]*name=[\"']viewport[\"']", doc, flags=re.I):
        doc = re.sub(
            r"(<head\b[^>]*>)",
            "\\1\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            doc,
            count=1,
            flags=re.I,
        )
    return doc


def _html_tag_with_lang(match: re.Match[str]) -> str:
    attrs = match.group(1)
    if re.search(r"\slang=", attrs, flags=re.I):
        return match.group(0)
    return f'<html lang="en"{attrs}>'


def _strip_managed_blocks(doc: str) -> str:
    doc = _strip_between(doc, SHELL_START, SHELL_END)
    doc = _strip_between(doc, FOOTER_START, FOOTER_END)
    doc = re.sub(
        rf"\s*<style\b[^>]*id=[\"']{TOKEN_STYLE_ID}[\"'][^>]*>.*?</style>\s*",
        "",
        doc,
        flags=re.I | re.S,
    )
    return doc


def _strip_between(doc: str, start: str, end: str) -> str:
    return re.sub(
        re.escape(start) + r".*?" + re.escape(end) + r"\s*",
        "",
        doc,
        flags=re.S,
    )


def _disable_legacy_runtime(doc: str, row: AuditLedgerRow) -> str:
    doc = re.sub(
        r"\s*<script\b[^>]*>.*?</script>\s*",
        "\n",
        doc,
        flags=re.I | re.S,
    )
    if row.route_boundary == "dev-audit-quarantine":
        doc = re.sub(r"<img\b[^>]*>", _disabled_media_placeholder, doc, flags=re.I)
        doc = re.sub(r"<source\b[^>]*>", "", doc, flags=re.I)
        doc = re.sub(
            r"<iframe\b[^>]*>.*?</iframe>",
            _disabled_media_placeholder,
            doc,
            flags=re.I | re.S,
        )
        doc = re.sub(r"<iframe\b[^>]*>", _disabled_media_placeholder, doc, flags=re.I)
        doc = re.sub(r"<link\b[^>]*>", _disable_link_if_asset, doc, flags=re.I)
    return doc


def _disabled_media_placeholder(match: re.Match[str]) -> str:
    label_match = re.search(r"\salt=(['\"])(.*?)\1", match.group(0), flags=re.I | re.S)
    label = html.escape(_trim_attr(label_match.group(2) if label_match else "captured media"))
    return (
        '<span class="apex-pro-disabled-asset" aria-hidden="true" '
        f'data-label="{label}"></span>'
    )


def _disable_link_if_asset(match: re.Match[str]) -> str:
    tag = match.group(0)
    rel_match = re.search(r"\srel=(['\"])(.*?)\1", tag, flags=re.I)
    rel = rel_match.group(2).lower() if rel_match else ""
    if any(token in rel for token in ("stylesheet", "icon", "preload", "preconnect")):
        return re.sub(r"\shref=", " data-apex-pro-disabled-href=", tag, flags=re.I)
    return tag


def _trim_attr(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:80]


def _upsert_title(doc: str, title: str) -> str:
    escaped = html.escape(title)
    if re.search(r"<title\b", doc, flags=re.I):
        return re.sub(
            r"<title\b[^>]*>.*?</title>",
            f"<title>{escaped}</title>",
            doc,
            count=1,
            flags=re.I | re.S,
        )
    return re.sub(r"(</head>)", f"  <title>{escaped}</title>\n\\1", doc, count=1, flags=re.I)


def _upsert_head_tokens(doc: str) -> str:
    return re.sub(
        r"(</head>)",
        "\n" + _token_style() + "\n\\1",
        doc,
        count=1,
        flags=re.I,
    )


def _upsert_body_attributes(doc: str, row: AuditLedgerRow) -> str:
    body_match = re.search(r"<body\b([^>]*)>", doc, flags=re.I)
    if not body_match:
        return doc
    attrs = body_match.group(1)
    attrs = re.sub(r"\sdata-apex-pro-[\w-]+=(\"[^\"]*\"|'[^']*')", "", attrs)
    attrs = _upsert_class(attrs, "apex-pro-route")
    attrs += ' data-apex-pro-hardened="true"'
    attrs += f' data-apex-pro-boundary="{html.escape(row.route_boundary)}"'
    attrs += f' data-apex-pro-family="{html.escape(row.family)}"'
    replacement = f"<body{attrs}>"
    return doc[: body_match.start()] + replacement + doc[body_match.end() :]


def _upsert_class(attrs: str, class_name: str) -> str:
    class_match = re.search(r"\sclass=(\"[^\"]*\"|'[^']*')", attrs, flags=re.I)
    if not class_match:
        return attrs + f' class="{class_name}"'
    quote = class_match.group(1)[0]
    classes = class_match.group(1).strip("\"'").split()
    if class_name not in classes:
        classes.append(class_name)
    value = quote + " ".join(classes) + quote
    return attrs[: class_match.start(1)] + value + attrs[class_match.end(1) :]


def _upsert_first_h1(doc: str, h1: str) -> str:
    escaped = html.escape(h1)
    if re.search(r"<h1\b", doc, flags=re.I):
        return re.sub(
            r"(<h1\b[^>]*>).*?(</h1>)",
            lambda match: match.group(1) + escaped + match.group(2),
            doc,
            count=1,
            flags=re.I | re.S,
        )
    return _insert_after_body_open(doc, f'<h1 class="apex-pro-generated-h1">{escaped}</h1>\n')


def _insert_after_body_open(doc: str, markup: str) -> str:
    return re.sub(r"(<body\b[^>]*>)", "\\1\n" + markup, doc, count=1, flags=re.I)


def _insert_before_body_close(doc: str, markup: str) -> str:
    if re.search(r"</body>", doc, flags=re.I):
        return re.sub(r"(</body>)", markup + "\n\\1", doc, count=1, flags=re.I)
    return doc + "\n" + markup


def _normalize_managed_spacing(doc: str) -> str:
    doc = re.sub(re.escape(SHELL_END) + r"\s+", SHELL_END + "\n", doc)
    doc = re.sub(re.escape(FOOTER_END) + r"\s+(</body>)", FOOTER_END + "\n\\1", doc, flags=re.I)
    return doc


def _shell_markup(row: AuditLedgerRow) -> str:
    status_label = _status_label(row)
    risk_state = _risk_state(row)
    safe_action = _safe_action(row)
    dev_banner = ""
    if row.route_boundary == "dev-audit-quarantine":
        dev_banner = (
            '<div class="apex-pro-dev-banner" role="note">'
            "<strong>Dev-only capture</strong>"
            "<span>This route is quarantined from production navigation.</span>"
            "</div>"
        )

    return f"""{SHELL_START}
<a class="apex-pro-skip" href="#apex-pro-route-content">Skip to content</a>
<aside class="apex-pro-nav" aria-label="Apex Pro route boundary">
  <a class="apex-pro-brand" href="/"><span>Dawnstrike</span><strong>Apex Pro</strong></a>
  <nav>
    <a href="/" aria-label="Open canonical Streamlit dashboard">Dashboard</a>
    <a href="#apex-pro-route-evidence">Evidence</a>
    <a href="#apex-pro-route-audit">Audit</a>
  </nav>
  <p>Static route archive. Production trading stays in the single operator dashboard.</p>
</aside>
<section class="apex-pro-statusbar" role="status" aria-live="polite">
  <span>{html.escape(status_label)}</span>
  <span>Freshness: route audit snapshot 2026-07-03</span>
  <span>Provider: local artifact manifest</span>
  <span>Focus: visible keyboard states enabled</span>
</section>
<header class="apex-pro-content-header" id="apex-pro-route-content">
  {dev_banner}
  <p class="apex-pro-kicker">{html.escape(row.family)} / {html.escape(row.route_boundary)}</p>
  <h1>{html.escape(row.derived_h1)}</h1>
  <div class="apex-pro-badge-row" aria-label="Route provenance">
    <span>Freshness: audit snapshot 2026-07-03</span>
    <span>Source/provider: {html.escape(_provider_label(row))}</span>
    <span>Model/strategy version: {html.escape(_model_label(row))}</span>
    <span>Risk state: {html.escape(risk_state)}</span>
  </div>
  <p class="apex-pro-next-action">Next safe action: {html.escape(safe_action)}</p>
</header>
<aside class="apex-pro-evidence-rail" id="apex-pro-route-evidence" aria-label="Evidence rail">
  <h2>Evidence</h2>
  <dl>
    <div><dt>Path</dt><dd>{html.escape(row.source)}</dd></div>
    <div><dt>Template</dt><dd>{html.escape(row.target_template)}</dd></div>
    <div><dt>Visual proof</dt><dd>{html.escape(row.visual_status)}</dd></div>
    <div><dt>Screenshot</dt><dd>{html.escape(row.screenshot_evidence or "n/a")}</dd></div>
  </dl>
</aside>
{SHELL_END}
"""


def _footer_markup(row: AuditLedgerRow) -> str:
    row_label = (
        f"Ledger row {row.index}; family {html.escape(row.family)}; "
        f"boundary {html.escape(row.route_boundary)}."
    )
    return f"""{FOOTER_START}
<footer class="apex-pro-audit-footer" id="apex-pro-route-audit">
  <strong>Audit trail</strong>
  <span>{row_label}</span>
  <span>Research-only. Live execution disabled. Missing truth must remain n/a or pending.</span>
</footer>
{FOOTER_END}"""


def _status_label(row: AuditLedgerRow) -> str:
    if row.route_boundary == "dev-audit-quarantine":
        return "Quarantined dev/audit route"
    if row.route_boundary.endswith("-archive") or row.route_boundary == "research-archive":
        return "Archived static route"
    return "Route under review"


def _risk_state(row: AuditLedgerRow) -> str:
    if row.route_boundary == "dev-audit-quarantine":
        return "dev-only; not trading evidence"
    if row.family == "paper_trade_basket_detail":
        return "paper/simulated; no live execution"
    if row.family in {"daily_session_detail", "monthly_calendar", "strategy_detail"}:
        return "research-only; requires fresh evidence"
    return "archive review required"


def _safe_action(row: AuditLedgerRow) -> str:
    if row.route_boundary == "dev-audit-quarantine":
        return "Use only for provider/parser debugging; do not trade from this page."
    if row.family == "paper_trade_basket_detail":
        return "Review the paper ticket in the canonical dashboard before any manual decision."
    if row.family == "strategy_detail":
        return "Compare strategy evidence in the dashboard; keep live execution disabled."
    if row.family in {"daily_session_detail", "monthly_calendar"}:
        return "Use as archived context, then verify current state in the dashboard."
    return "Open the canonical dashboard for current operator state."


def _provider_label(row: AuditLedgerRow) -> str:
    if row.route_boundary == "dev-audit-quarantine":
        return "dev fixture/provider capture"
    if "paper" in row.family:
        return "local PaperOps artifact"
    if row.family == "historical_backtest_report":
        return "local backtest artifact"
    return "local generated HTML artifact"


def _model_label(row: AuditLedgerRow) -> str:
    if row.family == "strategy_detail":
        return row.derived_h1.replace("Strategy tear sheet: ", "")
    if row.family == "paper_trade_basket_detail":
        return row.derived_h1.split(": ", 1)[-1]
    return row.target_template


def _token_style() -> str:
    return f"""<style id="{TOKEN_STYLE_ID}">
:root {{
  --apex-bg: #071013;
  --apex-panel: #0d171b;
  --apex-panel-2: #111f24;
  --apex-line: #24414a;
  --apex-text: #edf7f7;
  --apex-muted: #94abb0;
  --apex-accent: #47d7b2;
  --apex-warn: #f0bf5b;
  --apex-danger: #ff6f73;
  --apex-info: #70a7ff;
  --apex-radius: 8px;
  --apex-sidebar: 238px;
  --apex-rail: 292px;
}}
body.apex-pro-route {{
  background: var(--apex-bg);
  color: var(--apex-text);
  min-height: 100vh;
}}
body.apex-pro-route > aside.apex-shell,
body.apex-pro-route > aside.side-shell {{
  display: none !important;
}}
body.apex-pro-route > main {{
  margin-left: var(--apex-sidebar) !important;
  margin-right: var(--apex-rail) !important;
  max-width: none !important;
  padding-top: 150px !important;
  height: calc(100vh - 20px) !important;
  overflow: auto !important;
}}
body.apex-pro-route a:focus-visible,
body.apex-pro-route button:focus-visible,
body.apex-pro-route summary:focus-visible,
body.apex-pro-route [tabindex]:focus-visible {{
  outline: 3px solid var(--apex-accent) !important;
  outline-offset: 3px !important;
  box-shadow: 0 0 0 6px rgba(71, 215, 178, .18) !important;
}}
.apex-pro-skip {{
  position: fixed;
  top: 10px;
  left: 10px;
  z-index: 10000;
  transform: translateY(-160%);
  background: var(--apex-accent);
  color: #03100d;
  padding: 10px 12px;
  border-radius: var(--apex-radius);
  font-weight: 800;
}}
.apex-pro-skip:focus-visible {{ transform: translateY(0); }}
.apex-pro-nav {{
  position: fixed;
  inset: 0 auto 0 0;
  z-index: 9990;
  width: var(--apex-sidebar);
  box-sizing: border-box;
  padding: 18px;
  border-right: 1px solid var(--apex-line);
  background: linear-gradient(180deg, #071013, #0b171a);
}}
.apex-pro-brand {{
  display: block;
  color: var(--apex-text);
  text-decoration: none;
  margin-bottom: 18px;
}}
.apex-pro-brand span {{
  display: block;
  color: var(--apex-muted);
  font-size: 11px;
  letter-spacing: 0;
  text-transform: uppercase;
}}
.apex-pro-brand strong {{ display: block; font-size: 24px; }}
.apex-pro-nav nav {{
  display: grid;
  gap: 8px;
  margin: 18px 0;
}}
.apex-pro-nav nav a {{
  border: 1px solid var(--apex-line);
  border-radius: var(--apex-radius);
  padding: 10px 11px;
  color: var(--apex-text);
  background: var(--apex-panel);
  text-decoration: none;
}}
.apex-pro-nav p {{
  color: var(--apex-muted);
  font-size: 12px;
  line-height: 1.45;
}}
.apex-pro-statusbar {{
  position: fixed;
  z-index: 9985;
  left: var(--apex-sidebar);
  right: 0;
  top: 0;
  min-height: 42px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 8px 14px;
  border-bottom: 1px solid var(--apex-line);
  background: rgba(7, 16, 19, .96);
}}
.apex-pro-statusbar span,
.apex-pro-badge-row span {{
  border: 1px solid var(--apex-line);
  border-radius: 999px;
  padding: 6px 9px;
  color: var(--apex-text);
  background: var(--apex-panel-2);
  font-size: 12px;
}}
.apex-pro-content-header {{
  position: fixed;
  z-index: 9980;
  left: var(--apex-sidebar);
  right: var(--apex-rail);
  top: 43px;
  min-height: 88px;
  box-sizing: border-box;
  padding: 12px 16px;
  border-bottom: 1px solid var(--apex-line);
  background: rgba(9, 19, 22, .97);
}}
.apex-pro-kicker {{
  color: var(--apex-muted);
  font-size: 11px;
  margin: 0 0 5px;
  text-transform: uppercase;
}}
.apex-pro-content-header h1 {{
  margin: 0 0 8px;
  font-size: 24px;
  line-height: 1.12;
  letter-spacing: 0;
}}
.apex-pro-badge-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}}
.apex-pro-next-action {{
  color: var(--apex-muted);
  font-size: 13px;
  margin: 8px 0 0;
}}
.apex-pro-evidence-rail {{
  position: fixed;
  z-index: 9975;
  top: 43px;
  right: 0;
  bottom: 0;
  width: var(--apex-rail);
  box-sizing: border-box;
  overflow: auto;
  padding: 14px;
  border-left: 1px solid var(--apex-line);
  background: #091417;
}}
.apex-pro-evidence-rail h2 {{
  margin: 0 0 10px;
  font-size: 18px;
}}
.apex-pro-evidence-rail dl,
.apex-pro-evidence-rail div {{
  display: grid;
  gap: 7px;
}}
.apex-pro-evidence-rail div {{
  border: 1px solid var(--apex-line);
  border-radius: var(--apex-radius);
  background: var(--apex-panel);
  padding: 10px;
}}
.apex-pro-evidence-rail dt {{
  color: var(--apex-muted);
  font-size: 10px;
  text-transform: uppercase;
}}
.apex-pro-evidence-rail dd {{
  margin: 0;
  color: var(--apex-text);
  font-size: 12px;
  overflow-wrap: anywhere;
}}
.apex-pro-dev-banner {{
  display: flex;
  gap: 10px;
  align-items: center;
  border: 1px solid var(--apex-warn);
  border-radius: var(--apex-radius);
  background: rgba(240, 191, 91, .12);
  color: var(--apex-warn);
  padding: 8px 10px;
  margin-bottom: 8px;
}}
.apex-pro-audit-footer {{
  margin-left: var(--apex-sidebar);
  margin-right: var(--apex-rail);
  border-top: 1px solid var(--apex-line);
  background: #091417;
  color: var(--apex-muted);
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 14px 16px;
  font-size: 12px;
}}
.apex-pro-generated-h1 {{
  margin-left: var(--apex-sidebar);
  margin-right: var(--apex-rail);
  padding-top: 150px;
}}
@media (max-width: 1100px) {{
  :root {{ --apex-sidebar: 0px; --apex-rail: 0px; }}
  .apex-pro-nav,
  .apex-pro-evidence-rail {{
    position: static;
    width: auto;
    border: 0;
  }}
  .apex-pro-statusbar,
  .apex-pro-content-header {{
    position: static;
    left: auto;
    right: auto;
  }}
  body.apex-pro-route > main {{
    margin: 0 !important;
    padding-top: 16px !important;
    height: 72vh !important;
    overflow: auto !important;
  }}
  .apex-pro-audit-footer {{
    margin: 0;
  }}
}}
</style>"""
