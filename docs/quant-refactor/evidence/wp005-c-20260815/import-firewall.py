from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
CORE_MODULES = (
    "intraday_scanner.v2.opportunity",
    "intraday_scanner.v2.opportunity.models",
    "intraday_scanner.v2.opportunity.features",
    "intraday_scanner.v2.opportunity.discovery",
    "intraday_scanner.v2.opportunity.regimes",
    "intraday_scanner.v2.opportunity.registry",
    "intraday_scanner.v2.opportunity.ranking",
    "intraday_scanner.v2.opportunity.risk",
    "intraday_scanner.v2.opportunity.quality_gate",
    "intraday_scanner.v2.opportunity.pipeline",
    "intraday_scanner.storage.opportunity_store",
    "intraday_scanner.storage.opportunity_outcome_store",
    "intraday_scanner.storage.opportunity_miss_store",
    "intraday_scanner.storage.opportunity_metric_store",
    "app",
)

for module_name in CORE_MODULES:
    importlib.import_module(module_name)

loaded = sorted(name for name in sys.modules if "validation_robustness" in name)
if loaded:
    raise SystemExit(f"eager robustness imports detected: {loaded}")

opportunity = sys.modules["intraday_scanner.v2.opportunity"]
if hasattr(opportunity, "ValidationRobustnessReport"):
    raise SystemExit("package root eagerly exposes ValidationRobustnessReport")

source_files = tuple(
    sorted((ROOT / "intraday_scanner/v2/opportunity").glob("validation_robustness*.py"))
)
forbidden = (
    "alpha.v6",
    "backtest",
    "app",
    "broker",
    "network",
    "scheduler",
    "streamlit",
    "storage",
)
for source in source_files:
    if source.stat().st_size >= 40_000:
        raise SystemExit(f"module exceeds 40 KB: {source.relative_to(ROOT)}")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = tuple(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ) + tuple(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    blocked = sorted(
        {module for module in imports for token in forbidden if token in module}
    )
    if blocked:
        raise SystemExit(
            f"forbidden dependency in {source.relative_to(ROOT)}: {blocked}"
        )

print(f"core_modules_imported={len(CORE_MODULES)}")
print(f"robustness_modules_scanned={len(source_files)}")
print("eager_robustness_modules=0")
print("forbidden_dependencies=0")
print("package_root_exports=0")
