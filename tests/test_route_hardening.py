from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from intraday_scanner.dashboard.route_hardening import harden_html, harden_routes, is_hardened_html
from intraday_scanner.dashboard.ui_audit_ledger import build_ledger_rows


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")


def test_harden_html_replaces_generic_title_and_inserts_apex_shell(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        [
            {
                "group": ".pytest_full_tmp_1",
                "source": ".pytest_full_tmp_1/provider/raw_source.html",
                "title": "",
                "h1": "",
                "status": "ok",
                "mode": "desktop_fullpage",
                "width": 1440,
                "height": 1000,
                "screenshot": "screenshots/provider/raw_source.png",
            }
        ],
    )
    row = build_ledger_rows(
        repo_root=tmp_path,
        manifest_path=manifest_path,
        include_discovered_extras=False,
    )[0]
    html = "<!doctype html><html><head><title>Untitled</title></head><body><p>Raw</p></body></html>"

    hardened = harden_html(html, row)
    rehardened = harden_html(hardened, row)

    assert hardened == rehardened
    assert is_hardened_html(hardened)
    assert '<html lang="en">' in hardened
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in hardened
    assert "<title>Dawnstrike Dev Audit Quarantine" in hardened
    assert "Freshness: route audit snapshot 2026-07-03" in hardened
    assert "Source/provider: dev fixture/provider capture" in hardened
    assert "Risk state: dev-only; not trading evidence" in hardened
    assert "Next safe action:" in hardened
    assert "apex-pro-dev-banner" in hardened
    assert ":focus-visible" in hardened


def test_harden_routes_updates_source_and_ledger_detection(tmp_path: Path) -> None:
    repo_root = tmp_path
    source = "data/v2_interface_apex/trades/paper-2026-05-01-day-orb-5m-paper-basket-0.html"
    source_path = repo_root / source
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "<!doctype html><html><head><title>Dawnstrike Apex - Trade paper basket</title>"
        "</head><body><h1>paper basket</h1></body></html>",
        encoding="utf-8",
    )
    manifest_path = repo_root / "manifest.json"
    _write_manifest(
        manifest_path,
        [
            {
                "group": "data/v2_interface_apex",
                "source": source,
                "title": "Dawnstrike Apex - Trade paper basket",
                "h1": "paper basket",
                "status": "ok",
                "mode": "desktop_fullpage",
                "width": 1440,
                "height": 1000,
                "screenshot": "screenshots/trade.png",
            }
        ],
    )

    first = harden_routes(repo_root, manifest_path)
    second = harden_routes(repo_root, manifest_path)
    ledger = build_ledger_rows(
        repo_root=repo_root,
        manifest_path=manifest_path,
        include_discovered_extras=False,
    )
    text = source_path.read_text(encoding="utf-8")

    assert first[0].changed is True
    assert second[0].changed is False
    assert ledger[0].apex_pro_hardened is True
    assert "Dawnstrike Apex Pro Archive - 2026-05-01 Day ORB 5m Paper Basket 1" in text
    assert "Stamped with Apex Pro audit shell" in ledger[0].action_taken
    assert "paper-trade ticket needs explicit" not in ledger[0].remaining_exceptions


def test_current_manifest_routes_are_hardened_with_provenance() -> None:
    manifest_path = Path("data/ui_ux_audit_visuals_20260703T095541/manifest.json")
    if not manifest_path.exists():
        pytest.skip("latest visual audit manifest is not present in this checkout")

    rows = build_ledger_rows(
        repo_root=Path("."),
        manifest_path=manifest_path,
        include_discovered_extras=False,
    )

    assert len(rows) >= 825
    assert sum(1 for row in rows if row.apex_pro_hardened) == 825
    assert sum(1 for row in rows if row.route_boundary == "dev-audit-quarantine") == 116
    for row in rows:
        text = Path(row.source).read_text(encoding="utf-8")
        title_match = re.search(r"<title>(.*?)</title>", text, flags=re.I | re.S)
        assert title_match is not None, row.source
        title = title_match.group(1).strip()
        assert title == row.derived_title
        assert title.lower() != "untitled"
        assert not title.endswith("Trade paper basket")
        assert 'data-apex-pro-hardened="true"' in text
        assert "Freshness: route audit snapshot 2026-07-03" in text
        assert "Source/provider:" in text
        assert "Risk state:" in text
        assert "Next safe action:" in text
        assert "Audit trail" in text
        assert ":focus-visible" in text
        if row.route_boundary == "dev-audit-quarantine":
            assert "apex-pro-dev-banner" in text
