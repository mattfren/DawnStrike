from __future__ import annotations

import ast
import json
from pathlib import Path

from intraday_scanner.v2.command_center_x.adapters import (
    build_view_models,
    system_health_view,
    today_view,
)
from intraday_scanner.v2.command_center_x.core import (
    build_command_center_x,
    inventory_command_center_x,
    qa_command_center_x,
    report_command_center_x,
    verify_command_center_x,
)
from intraday_scanner.v2.command_center_x.qa import REQUIRED_PAGE_NAMES, run_command_center_x_qa

REPO_ROOT = Path(".")


def _prepare_existing_command_center_sibling(output_root: Path) -> None:
    sibling = output_root.parent / "v2_command_center"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "index.html").write_text(
        "<!doctype html><title>Existing</title>\n", encoding="utf-8"
    )


def test_inventory_command_writes_repo_truth_maps(tmp_path: Path) -> None:
    output_root = tmp_path / "command_center_x"

    result = inventory_command_center_x(repo_root=REPO_ROOT, output_root=output_root)
    inventory = json.loads((output_root / "reports/repo_inventory.json").read_text())

    assert result["status"] == "passed"
    assert (REPO_ROOT / "docs/repo_inventory/dawnstrike_repo_inventory.md").exists()
    assert (REPO_ROOT / "docs/repo_inventory/dawnstrike_cli_map.md").exists()
    assert (REPO_ROOT / "docs/repo_inventory/dawnstrike_artifact_map.md").exists()
    assert any(
        row["module"] == "intraday_scanner.v2.omega_sentinel" for row in inventory["cli_commands"]
    )
    assert any(
        row["module"] == "intraday_scanner.v2.command_center_x" for row in inventory["cli_commands"]
    )
    assert any(row["path"] == "data/v2_command_center" for row in inventory["data_artifacts"])
    assert any(
        row["path"] == "intraday_scanner/v2/command_center_x" for row in inventory["v2_modules"]
    )


def test_adapters_handle_missing_artifacts_without_fabricating_values(tmp_path: Path) -> None:
    system = system_health_view(repo_root=tmp_path)
    today = today_view(repo_root=tmp_path)
    views = build_view_models(repo_root=tmp_path)

    assert system["sentinel_status"] == "missing"
    assert system["provider_readiness"] == "missing"
    assert system["live_trading_disabled"] is True
    assert all(ref["exists"] is False for ref in system["source_artifacts"])
    assert today["accepted_count"] == 0
    assert today["blocked_count"] == 0
    assert today["no_pick_reasons"] == [
        "No latest no-picks explanation found in Telegram artifacts."
    ]
    assert views["repo_inventory"]["status"] == "missing"
    assert (
        "Command Center X inventory has not been generated." in views["repo_inventory"]["warnings"]
    )


def test_build_generates_required_pages_assets_view_models_and_bridge(tmp_path: Path) -> None:
    output_root = tmp_path / "command_center_x"
    _prepare_existing_command_center_sibling(output_root)
    manifest_path = REPO_ROOT / "data/v2_command_center/command_center_manifest.json"
    existing_manifest = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""

    inventory_command_center_x(repo_root=REPO_ROOT, output_root=output_root)
    build = build_command_center_x(repo_root=REPO_ROOT, output_root=output_root)
    qa = qa_command_center_x(repo_root=REPO_ROOT, output_root=output_root)
    report = report_command_center_x(repo_root=REPO_ROOT, output_root=output_root)
    verify = verify_command_center_x(repo_root=REPO_ROOT, output_root=output_root)

    assert build["status"] == "passed"
    assert qa["status"] == "passed"
    assert report["final_status"] == "COMPLETE_COMMAND_CENTER_X"
    assert verify["status"] == "passed"
    assert (output_root / "index.html").exists()
    assert (output_root / "assets/command_center_x.css").exists()
    assert (output_root / "assets/design_tokens.json").exists()
    assert (output_root / "data/system_health.json").exists()
    assert (output_root / "data/today.json").exists()
    for name in REQUIRED_PAGE_NAMES:
        assert (output_root / "pages" / name).exists(), name
    page_text = (output_root / "pages/system_map.html").read_text(encoding="utf-8")
    assert "Research-only / paper-only" in page_text
    assert "Live trading disabled" in page_text
    assert (REPO_ROOT / "data/v2_command_center/command_center_x.html").exists()
    if existing_manifest:
        assert manifest_path.read_text(encoding="utf-8") == existing_manifest


def test_qa_rejects_tampered_output(tmp_path: Path) -> None:
    output_root = tmp_path / "command_center_x"
    _prepare_existing_command_center_sibling(output_root)
    inventory_command_center_x(repo_root=REPO_ROOT, output_root=output_root)
    build_command_center_x(repo_root=REPO_ROOT, output_root=output_root)

    target = output_root / "pages/today.html"
    target.write_text(
        target.read_text(encoding="utf-8")
        + '\n<script src="https://cdn.example.invalid/x.js"></script>\n'
        + "TELEGRAM_BOT_TOKEN\n"
        + r"C:\Users\MattFields\secret"
        + '\n<span data-trust="validated">Validated</span>\n',
        encoding="utf-8",
        newline="\n",
    )
    qa = run_command_center_x_qa(output_root=output_root, repo_root=REPO_ROOT)

    assert qa["status"] == "failed"
    assert qa["checks"]["script_tags_clear"] is False
    assert qa["checks"]["external_dependencies_clear"] is False
    assert qa["checks"]["secret_values_clear"] is False
    assert qa["checks"]["invalid_validated_badges_clear"] is False
    assert qa["checks"]["absolute_path_leaks_clear"] is False


def test_command_center_x_package_has_read_only_local_import_surface() -> None:
    forbidden_roots = {
        "app",
        "sqlite3",
        "streamlit",
        "socket",
        "urllib",
        "requests",
        "httpx",
    }
    forbidden_calls = {"connect", "urlopen", "request"}

    for path in Path("intraday_scanner/v2/command_center_x").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_roots, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_roots, path
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path
