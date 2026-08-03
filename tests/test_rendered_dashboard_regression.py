from __future__ import annotations

from pathlib import Path


def test_calendar_and_v6_research_assets_keep_truth_boundary() -> None:
    root = Path(__file__).parents[1]
    javascript = (root / "web" / "assets" / "dawnstrike.js").read_text(encoding="utf-8")
    html = (root / "web" / "index.html").read_text(encoding="utf-8")

    assert "calendar" in javascript.lower()
    assert "v6-learning.json" in javascript
    assert "Decision Replay" in html
    assert "v6-promotion-gates" in html
    assert "v6-account-comparison" in html
    assert "Account comparison" in html
    assert "prediction_visible" in javascript
    assert "Prediction hidden until the evidence gate passes" in javascript
    assert 'role="grid"' not in html
    assert 'role="gridcell"' not in javascript
    assert 'aria-pressed="${selected}"' in javascript
    assert 'region.setAttribute("tabindex", "0")' in javascript
    assert r"C:\r\dawnstrike" not in javascript
    assert "/home/" not in javascript
