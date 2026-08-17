"""AST and fresh-process dependency firewall for the WP007 projection boundary."""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return tuple(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ) + tuple(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )


checks: list[tuple[Path, tuple[str, ...], str]] = []
core_files = tuple(sorted((ROOT / "intraday_scanner" / "v2" / "opportunity").glob("*.py")))
for path in core_files:
    storage_token = (
        ()
        if path.name in {"outcome_persistence.py", "outcome_replay.py"}
        else ("intraday_scanner.storage",)
    )
    checks.append(
        (
            path,
            (
                "intraday_scanner.dashboard",
                *storage_token,
                "intraday_scanner.services",
                "streamlit",
                "sqlite3",
            ),
            "opportunity core reverse dependency",
        )
    )

projection_core = ROOT / "intraday_scanner" / "dashboard" / "opportunity_projection.py"
checks.append(
    (
        projection_core,
        (
            "intraday_scanner.storage",
            "intraday_scanner.services",
            "streamlit",
            "sqlite3",
        ),
        "projection contract side effect dependency",
    )
)
renderer = ROOT / "intraday_scanner" / "dashboard" / "opportunity_projection_render.py"
checks.append(
    (
        renderer,
        ("intraday_scanner.storage", "streamlit", "sqlite3"),
        "projection renderer persistence dependency",
    )
)
store_adapter = (
    ROOT / "intraday_scanner" / "dashboard" / "opportunity_projection_store.py"
)
checks.append(
    (
        store_adapter,
        ("streamlit", "intraday_scanner.providers", "intraday_scanner.services"),
        "projection adapter product or provider dependency",
    )
)
accepted_storage = tuple(
    sorted((ROOT / "intraday_scanner" / "storage").glob("opportunity*.py"))
)
for path in accepted_storage:
    checks.append(
        (
            path,
            ("intraday_scanner.dashboard",),
            "accepted storage reverse dependency",
        )
    )

violations: list[dict[str, str]] = []
accepted_core_storage_imports = {
    "producer.py": {
        "intraday_scanner.storage.opportunity_store",
        "intraday_scanner.storage.test_isolation",
    }
}
for path, forbidden, label in checks:
    for imported in _imports(path):
        if imported in accepted_core_storage_imports.get(path.name, set()):
            continue
        if any(imported == token or imported.startswith(f"{token}.") for token in forbidden):
            violations.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "import": imported,
                    "rule": label,
                }
            )

importlib.import_module("intraday_scanner.v2.opportunity.pipeline")
core_dashboard_loaded = tuple(
    sorted(name for name in sys.modules if name.startswith("intraday_scanner.dashboard"))
)
importlib.import_module("intraday_scanner.dashboard.opportunity_projection")
projection_forbidden_loaded = tuple(
    sorted(
        name
        for name in sys.modules
        if name == "streamlit" or name.startswith("intraday_scanner.storage")
    )
)
importlib.import_module("intraday_scanner.dashboard.opportunity_projection_render")
renderer_streamlit_loaded = "streamlit" in sys.modules
importlib.import_module("intraday_scanner.dashboard.opportunity_projection_store")
adapter_product_loaded = tuple(
    sorted(
        name
        for name in sys.modules
        if name == "streamlit"
        or name.startswith("intraday_scanner.providers")
        or name.startswith("intraday_scanner.services")
    )
)

result = {
    "ast_file_count": len(checks),
    "ast_violations": violations,
    "core_dashboard_modules_loaded": core_dashboard_loaded,
    "projection_forbidden_modules_loaded": projection_forbidden_loaded,
    "renderer_loaded_streamlit": renderer_streamlit_loaded,
    "adapter_product_modules_loaded": adapter_product_loaded,
}
print(json.dumps(result, indent=2, sort_keys=True))
if (
    violations
    or core_dashboard_loaded
    or projection_forbidden_loaded
    or renderer_streamlit_loaded
    or adapter_product_loaded
):
    raise SystemExit(1)
