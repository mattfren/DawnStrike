from __future__ import annotations

import json
from pathlib import Path

import pytest

from intraday_scanner.dashboard.ui_audit_ledger import (
    build_ledger_rows,
    derive_route_metadata,
    route_boundary,
    route_family,
    write_ledger_json,
    write_ledger_markdown,
)


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")


def test_trade_basket_metadata_derives_unique_non_generic_titles() -> None:
    first = "data/v2_interface_apex/trades/paper-2026-05-01-day-orb-5m-paper-basket-0.html"
    second = "data/v2_interface_apex/trades/paper-2026-05-01-day-orb-5m-paper-basket-1.html"

    first_title, first_h1 = derive_route_metadata(first, "Dawnstrike Apex - Trade paper basket")
    second_title, second_h1 = derive_route_metadata(second, "Dawnstrike Apex - Trade paper basket")

    assert first_title == "Dawnstrike Apex Pro Archive - 2026-05-01 Day ORB 5m Paper Basket 1"
    assert second_title == "Dawnstrike Apex Pro Archive - 2026-05-01 Day ORB 5m Paper Basket 2"
    assert first_title != second_title
    assert "Trade paper basket" not in first_title
    assert first_h1 == "2026-05-01 paper basket 1: Day ORB 5m"
    assert second_h1 == "2026-05-01 paper basket 2: Day ORB 5m"


def test_dev_provider_capture_routes_are_quarantined() -> None:
    source = ".pytest_full_tmp_1/test_marketwatch_fixture_repor0/marketwatch/raw_source.html"

    assert route_family(source) == "raw_fixture_or_provider_capture"
    assert route_boundary(source) == "dev-audit-quarantine"

    title, h1 = derive_route_metadata(source)
    assert title == "Dawnstrike Dev Audit Quarantine - Marketwatch Capture"
    assert h1 == "Dev-only provider capture: Marketwatch"


def test_ledger_writes_manifest_rows_and_repo_extra_routes(tmp_path: Path) -> None:
    repo_root = tmp_path
    manifest_path = repo_root / "manifest.json"
    trade_source = (
        "data/v2_interface_apex/trades/"
        "paper-2026-05-01-bullish-fvg-continuation-paper-basket-0.html"
    )
    extra_source = "data/v2_command_center_x3/pages/home.html"
    (repo_root / trade_source).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / trade_source).write_text("<html><title>old</title></html>", encoding="utf-8")
    (repo_root / extra_source).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / extra_source).write_text("<html><title>home</title></html>", encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            {
                "group": "data/v2_interface_apex",
                "source": trade_source,
                "title": "Dawnstrike Apex - Trade paper basket",
                "h1": "paper basket through bullish_fvg_continuation",
                "status": "ok",
                "mode": "desktop_fullpage",
                "width": 1440,
                "height": 1000,
                "screenshot": "screenshots/trade.png",
            }
        ],
    )

    rows = build_ledger_rows(repo_root=repo_root, manifest_path=manifest_path)
    json_path = repo_root / "ledger.json"
    markdown_path = repo_root / "UI_AUDIT_COMPLETION.md"
    write_ledger_json(rows, json_path)
    write_ledger_markdown(
        rows,
        markdown_path,
        manifest_path=manifest_path.relative_to(repo_root),
        json_path=json_path.relative_to(repo_root),
    )

    assert [row.ledger_origin for row in rows] == ["manifest", "repo-extra"]
    assert rows[0].derived_title.endswith("Bullish FVG Continuation Paper Basket 1")
    assert rows[0].route_boundary == "apex-static-archive"
    assert rows[1].route_boundary == "legacy-command-center-archive"
    assert "Trade paper basket" not in rows[0].derived_title
    assert "| 2 | data/v2_command_center_x3/pages/home.html" in markdown_path.read_text(
        encoding="utf-8"
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["row_count"] == 2


def test_current_visual_manifest_has_minimum_rows_and_replacement_titles() -> None:
    manifest_path = Path("data/ui_ux_audit_visuals_20260703T095541/manifest.json")
    if not manifest_path.exists():
        pytest.skip("latest visual audit manifest is not present in this checkout")

    rows = build_ledger_rows(
        repo_root=Path("."),
        manifest_path=manifest_path,
        include_discovered_extras=False,
    )

    assert len(rows) >= 825
    assert sum(1 for row in rows if row.visual_status.startswith("ok;")) == 825
    assert not [row for row in rows if row.derived_title.lower() == "untitled"]
    assert not [
        row
        for row in rows
        if row.family != "raw_fixture_or_provider_capture"
        and row.derived_title.endswith("Trade paper basket")
    ]
