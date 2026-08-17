from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
TOKENS = (
    "opportunity_validation",
    "validation_robustness",
)
PATH_TOKENS = (
    "app.py",
    "broker",
    "network",
    "provider",
    "scheduler",
    "streamlit",
    "runtime",
)

files = [ROOT / "app.py"]
files.extend(
    path
    for path in (ROOT / "intraday_scanner").rglob("*.py")
    if any(token in str(path.relative_to(ROOT)).lower() for token in PATH_TOKENS)
)
violations: list[dict[str, str]] = []
for path in sorted(set(files)):
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
    for imported in imports:
        if any(token in imported for token in TOKENS):
            violations.append(
                {"file": str(path.relative_to(ROOT)), "import": imported}
            )

for module in (
    "intraday_scanner.v2.opportunity.pipeline",
    "intraday_scanner.storage",
    "intraday_scanner.storage.opportunity_store",
    "intraday_scanner.storage.opportunity_outcome_store",
    "intraday_scanner.storage.opportunity_miss_store",
    "intraday_scanner.storage.opportunity_metric_store",
):
    importlib.import_module(module)
loaded = tuple(
    sorted(name for name in sys.modules if any(token in name for token in TOKENS))
)
result = {
    "ast_file_count": len(set(files)),
    "ast_violations": violations,
    "fresh_process_forbidden_modules_loaded": loaded,
}
print(json.dumps(result, indent=2, sort_keys=True))
if violations or loaded:
    raise SystemExit(1)
