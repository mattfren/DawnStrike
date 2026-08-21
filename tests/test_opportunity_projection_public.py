from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _javascript_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    opening_brace = source.index("{", start)
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"JavaScript function is not balanced: {name}")


def test_static_projection_is_inside_existing_overview_and_nav_is_unchanged() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    overview = html.index('id="view-overview"')
    projection = html.index('id="opportunity-projection"')
    calendar = html.index('id="view-calendar"')

    assert overview < projection < calendar
    assert html.count('class="nav-link') == 6
    assert html.count('class="page-view') == 6
    assert 'id="opportunity-projection"' in html
    assert "Today's Best Opportunities" in html
    assert "hidden" in html[projection : projection + 180]


def test_static_renderer_uses_dom_construction_not_persisted_inner_html() -> None:
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    renderer = _javascript_function(source, "renderOpportunityProjection")

    assert "document.createElement" in renderer
    assert ".textContent" in renderer
    assert ".innerHTML" not in renderer
    assert ".slice(0, 5)" in renderer
    assert 'loadJson("/data/opportunity-projection.json", request)' in source
    assert "NO QUALIFYING TRADE CURRENTLY EXISTS." in renderer


def test_static_renderer_treats_persisted_markup_as_text_and_hides_disabled() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the public projection DOM contract"
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _javascript_function(source, name)
        for name in (
            "renderOpportunityProjection",
            "appendOpportunityField",
            "opportunityStateLabel",
            "projectionValue",
            "projectionList",
        )
    )
    probe = f"""
class Element {{
  constructor(id) {{
    this.id=id; this.children=[]; this.hidden=false; this.textContent=""; this.className="";
  }}
  appendChild(child) {{ this.children.push(child); }}
  append(...children) {{ this.children.push(...children); }}
  replaceChildren() {{ this.children=[]; }}
}}
const ids = Object.fromEntries([
  "opportunity-projection",
  "opportunity-projection-state",
  "opportunity-projection-message",
  "opportunity-projection-rows",
].map((id) => [id, new Element(id)]));
const document = {{
  getElementById: (id) => ids[id],
  createElement: (name) => new Element(name),
}};
const state = {{ opportunityProjection: {{
  state: "QUALIFYING",
  message: "<img src=x onerror=alert(1)>",
  rows: [{{
    rank: 1, symbol: "<script>alert(1)</script>", strategy_id: "DS-X", strategy_version: "1",
    direction: "long", decision: "watch", lifecycle: "experimental", evidence_kind: "heuristic",
    validation_wording: "not validated", market_regime: "unknown",
    market_regime_evidence_kind: "heuristic",
    security_regime: "unknown", security_regime_evidence_kind: "heuristic", triggered_anomalies: [],
    liquidity_score: null, liquidity_evidence_kind: null, why: ["<svg onload=alert(1)>"], risks: [],
    vetoes: [], entry_price: null, invalidation_price: null, target_price: null, limitations: [],
  }}],
}} }};
{functions}
renderOpportunityProjection();
const qualifying = {{
  hidden: ids["opportunity-projection"].hidden,
  message: ids["opportunity-projection-message"].textContent,
  tree: JSON.stringify(ids["opportunity-projection-rows"]),
}};
state.opportunityProjection = {{ state: "DISABLED", rows: [] }};
renderOpportunityProjection();
console.log(JSON.stringify({{ qualifying, disabledHidden: ids["opportunity-projection"].hidden }}));
"""
    completed = subprocess.run(
        [node, "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["qualifying"]["hidden"] is False
    assert result["qualifying"]["message"] == "<img src=x onerror=alert(1)>"
    assert "<script>alert(1)</script>" in result["qualifying"]["tree"]
    assert "<svg onload=alert(1)>" in result["qualifying"]["tree"]
    assert result["disabledHidden"] is True


def test_public_build_and_verifier_bind_projection_payload_and_manifest() -> None:
    build = Path("scripts/build_public.py").read_text(encoding="utf-8")
    verifier = Path("scripts/verify_public_artifact.py").read_text(encoding="utf-8")

    assert "load_latest_opportunity_projection(db_path)" in build
    assert "write_public_opportunity_projection" in build
    assert 'output_root / "data"' in build
    assert '"data/opportunity-projection.json"' in verifier
    assert '"data/opportunity-projection.json.manifest.json"' in verifier
    assert "opportunity_hash_mismatch" in verifier
    assert "opportunity_row_limit_exceeded" in verifier
    assert "opportunity_execution_boundary_invalid" in verifier
