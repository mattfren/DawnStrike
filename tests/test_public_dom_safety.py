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


def test_scenario_renderer_uses_hardened_helpers() -> None:
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")

    assert "scenarioEventTrailHtml(lifecycle.events)" in source
    assert "safeHttpsSourceUrl(record.source_url)" in source
    assert 'href="${escapeHtml(safeSourceUrl)}"' in source
    assert 'href="${escapeHtml(record.source_url)}"' not in source
    assert 'rel="noopener noreferrer"' in source


def test_scenario_security_helpers_escape_markup_and_reject_unsafe_urls() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the public DOM security contract"
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    helpers = "\n".join(
        _javascript_function(source, name)
        for name in (
            "humanizeIdentifier",
            "formatTimestamp",
            "escapeHtml",
            "scenarioEventTrailHtml",
            "safeHttpsSourceUrl",
        )
    )
    probe = f"""
{helpers}
const unsafe = [
  "javascript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "http://news.example.com/story",
  "//news.example.com/story",
  `https://user${":"}pass@news.example.com/story`,
  "https://@news.example.com/story",
  "https:///story",
  "https:\\\\news.example.com\\story",
  "https://news.example.com/\\u0000story",
];
const result = {{
  accepted: safeHttpsSourceUrl("  https://news.example.com/story?id=42  "),
  rejected: unsafe.map((value) => safeHttpsSourceUrl(value)),
  escaped: escapeHtml(`<img src=x onerror="alert(1)">'&`),
  trail: scenarioEventTrailHtml([{{
    event_type: "<img src=x onerror=alert(1)>",
    event_timestamp: "<svg onload=alert(1)>",
  }}]),
}};
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        [node, "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["accepted"] == "https://news.example.com/story?id=42"
    assert result["rejected"] == [None] * 9
    assert result["escaped"] == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;&amp;"
    assert "<img" not in result["trail"]
    assert "<svg" not in result["trail"]
    assert "&lt;Img" in result["trail"]
    assert "&lt;svg" in result["trail"]
