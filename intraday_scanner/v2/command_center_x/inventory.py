"""Repository truth inventory for Command Center X."""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAJOR_DATA_DIRS = (
    "data/v2_alpha_lab",
    "data/v2_data_truth",
    "data/v2_autodata",
    "data/v2_real_intraday",
    "data/v2_fill_truth",
    "data/v2_evidence_commit",
    "data/v2_paper_ops",
    "data/v2_forward_evidence",
    "data/v2_omega",
    "data/v2_omega_sentinel",
    "data/v2_learning_foundry",
    "data/v2_market_masters",
    "data/v2_telegram_intel",
    "data/v2_autonomous_runner",
    "data/v2_scheduler",
    "data/v2_command_center",
)

CLI_MODULES = (
    "intraday_scanner.cli",
    "intraday_scanner.v2.omega_sentinel",
    "intraday_scanner.v2.autodata",
    "intraday_scanner.v2.fill_truth",
    "intraday_scanner.v2.evidence_commit",
    "intraday_scanner.v2.paper_ops",
    "intraday_scanner.v2.learning_foundry",
    "intraday_scanner.v2.market_masters",
    "intraday_scanner.v2.telegram_intel",
    "intraday_scanner.v2.autonomous_runner",
    "intraday_scanner.v2.command_center",
    "intraday_scanner.v2.command_center_x",
)


def build_repo_inventory(
    *,
    repo_root: Path = Path("."),
    output_root: Path = Path("data/v2_command_center_x"),
) -> dict[str, Any]:
    """Inspect the current checkout and write inventory docs/reports."""
    repo_root = repo_root.resolve()
    _ensure_inventory_dirs(repo_root=repo_root, output_root=output_root)
    payload: dict[str, Any] = {
        "schema_version": "v2.command_center_x.inventory.v1",
        "build_id": _build_id("repo_inventory"),
        "created_at": _now(),
        "repo_root_name": repo_root.name,
        "git": _git_state(repo_root),
        "top_level": _top_level(repo_root),
        "python_packages": _python_packages(repo_root),
        "v2_modules": _v2_modules(repo_root),
        "cli_commands": _cli_commands(repo_root),
        "data_artifacts": _data_artifacts(repo_root),
        "tests": _tests(repo_root),
        "docs": _docs(repo_root),
        "existing_command_center": _existing_command_center(repo_root),
        "scripts": _scripts(repo_root),
        "current_risks": _current_risks(repo_root),
    }
    reports_dir = output_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    _write_json(reports_dir / "repo_inventory.json", payload)
    _write_inventory_docs(repo_root=repo_root, payload=payload)
    return payload


def _ensure_inventory_dirs(*, repo_root: Path, output_root: Path) -> None:
    for path in (
        output_root / "reports",
        output_root / "data",
        repo_root / "docs/repo_inventory",
    ):
        path.mkdir(parents=True, exist_ok=True)


def _git_state(repo_root: Path) -> dict[str, Any]:
    status = _run(repo_root, ["git", "status", "--short", "--branch"])
    diff_stat = _run(repo_root, ["git", "diff", "--stat"])
    return {
        "status_exit_code": status["exit_code"],
        "status_lines": status["stdout"].splitlines(),
        "diff_stat_exit_code": diff_stat["exit_code"],
        "diff_stat": diff_stat["stdout"].splitlines(),
        "dirty": any(line and not line.startswith("##") for line in status["stdout"].splitlines()),
    }


def _top_level(repo_root: Path) -> dict[str, Any]:
    dirs: list[str] = []
    files: list[str] = []
    for item in sorted(repo_root.iterdir(), key=lambda path: path.name.lower()):
        if item.name == ".git":
            continue
        if item.is_dir():
            dirs.append(item.name)
        elif item.is_file():
            files.append(item.name)
    return {
        "directories": dirs,
        "files": files,
        "major_config_files": [
            name for name in files if name in {"pyproject.toml", "README.md", ".gitignore"}
        ],
        "has_env_file": ".env" in files,
        "has_api_keys_text": "API keys.txt" in files,
    }


def _python_packages(repo_root: Path) -> list[dict[str, Any]]:
    root = repo_root / "intraday_scanner"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name) if root.exists() else []:
        if path.name.startswith("__"):
            continue
        if path.is_dir():
            py_files = sorted(path.rglob("*.py"))
            rows.append(
                {
                    "path": _rel(path, repo_root),
                    "purpose": _purpose_from_name(path.name),
                    "python_file_count": len(py_files),
                    "primary_symbols": _symbols(py_files[:8]),
                    "risk_level": _risk_level(path),
                    "ui_relevance": _ui_relevance(path),
                }
            )
        elif path.suffix == ".py":
            rows.append(
                {
                    "path": _rel(path, repo_root),
                    "purpose": _purpose_from_name(path.stem),
                    "python_file_count": 1,
                    "primary_symbols": _symbols([path]),
                    "risk_level": _risk_level(path),
                    "ui_relevance": _ui_relevance(path),
                }
            )
    return rows


def _v2_modules(repo_root: Path) -> list[dict[str, Any]]:
    root = repo_root / "intraday_scanner/v2"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name) if root.exists() else []:
        if path.name.startswith("__"):
            continue
        if not path.is_dir():
            continue
        py_files = sorted(path.rglob("*.py"))
        command = f"py -m intraday_scanner.v2.{path.name}"
        rows.append(
            {
                "path": _rel(path, repo_root),
                "purpose": _purpose_from_name(path.name),
                "python_file_count": len(py_files),
                "has_cli": (path / "__main__.py").exists(),
                "cli_command": command if (path / "__main__.py").exists() else None,
                "primary_symbols": _symbols(py_files[:10]),
                "input_artifacts": _guess_artifacts(path.name, input_side=True),
                "output_artifacts": _guess_artifacts(path.name, input_side=False),
                "risk_level": _risk_level(path),
                "ui_relevance": _ui_relevance(path),
            }
        )
    return rows


def _cli_commands(repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for module in CLI_MODULES:
        result = _run(repo_root, ["py", "-m", module, "--help"], timeout=45)
        help_text = (result["stdout"] + "\n" + result["stderr"]).strip()
        rows.append(
            {
                "command": f"py -m {module} --help",
                "module": module,
                "exists": result["exit_code"] == 0,
                "exit_code": result["exit_code"],
                "usage": _usage(help_text),
                "subcommands": _subcommands(help_text),
                "purpose": _purpose_from_name(module.rsplit(".", 1)[-1]),
                "safe_to_run_daily": _safe_daily(module),
                "mutates_state": _mutates_state(module),
                "writes_artifacts": True,
                "live_trading_risk": False,
                "ui_page": _cli_ui_page(module),
            }
        )
    return rows


def _data_artifacts(repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in MAJOR_DATA_DIRS:
        root = repo_root / raw
        files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
        dirs = [path for path in root.rglob("*") if path.is_dir()] if root.exists() else []
        latest = sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:12]
        rows.append(
            {
                "path": raw,
                "exists": root.exists(),
                "purpose": _purpose_from_name(Path(raw).name.removeprefix("v2_")),
                "file_count": len(files),
                "dir_count": len(dirs),
                "latest_files": [_rel(path, repo_root) for path in latest],
                "latest_status_files": _matching(
                    files, ("status", "readiness", "verify"), repo_root
                ),
                "latest_reports": _matching(files, ("report", "summary", "scorecard"), repo_root),
                "latest_manifests": _matching(files, ("manifest",), repo_root),
                "trust_level": _trust_level(raw),
                "stale_risk": _stale_risk(files),
                "ui_pages": _artifact_ui_pages(raw),
            }
        )
    return rows


def _tests(repo_root: Path) -> list[dict[str, Any]]:
    root = repo_root / "tests"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("test_*.py")) if root.exists() else []:
        text = path.read_text(encoding="utf-8", errors="ignore")
        rows.append(
            {
                "path": _rel(path, repo_root),
                "area": path.stem.removeprefix("test_").replace("_", " "),
                "test_count": text.count("def test_"),
                "covers_ui": "dashboard" in path.name or "command_center" in text,
                "covers_safety": any(
                    token in text for token in ("safety", "live_execution", "forbidden")
                ),
                "recommended_command_center_x_gap": _test_gap(path.name, text),
            }
        )
    return rows


def _docs(repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in (
        "docs",
        "docs/audit",
        "docs/audits",
        "docs/architecture",
        "docs/operations",
        "docs/research",
        "docs/agents",
    ):
        root = repo_root / raw
        files = sorted(root.rglob("*.md")) if root.exists() else []
        rows.append(
            {
                "path": raw,
                "exists": root.exists(),
                "markdown_count": len(files),
                "latest_files": [_rel(path, repo_root) for path in files[-12:]],
                "operator_docs": [path.name for path in files if "operator" in path.name.lower()],
                "audit_docs": [path.name for path in files if "audit" in path.as_posix().lower()][
                    -20:
                ],
                "stale_risk": "historical audit docs may not match current source"
                if files
                else "missing",
            }
        )
    return rows


def _existing_command_center(repo_root: Path) -> dict[str, Any]:
    root = repo_root / "data/v2_command_center"
    pages = sorted(root.glob("*.html")) if root.exists() else []
    qa = _read_json(root / "command_center_qa.json", {})
    manifest = _read_json(root / "command_center_manifest.json", {})
    return {
        "exists": root.exists(),
        "page_count": len(pages),
        "pages": [_rel(path, repo_root) for path in pages],
        "qa_status": qa.get("status", "missing"),
        "qa": qa,
        "manifest": manifest,
        "limitations": [
            "Dense technical page list rather than task-first IA.",
            "Limited normalized view models for Today, no-picks, trust, and next actions.",
            "Visual system is functional but not the Command Center X product surface.",
        ],
    }


def _scripts(repo_root: Path) -> list[dict[str, Any]]:
    root = repo_root / "scripts"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*")) if root.exists() else []:
        if path.is_file():
            rows.append(
                {
                    "path": _rel(path, repo_root),
                    "extension": path.suffix,
                    "purpose": _purpose_from_name(path.stem),
                    "ui_relevance": _ui_relevance(path),
                }
            )
    return rows


def _current_risks(repo_root: Path) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    if (repo_root / ".env").exists():
        risks.append(
            {
                "risk": "Local .env exists",
                "severity": "high",
                "ui_requirement": "Do not read, print, copy, or link secret-bearing files.",
            }
        )
    if (repo_root / "API keys.txt").exists():
        risks.append(
            {
                "risk": "API keys.txt exists in repo root",
                "severity": "high",
                "ui_requirement": "Do not read, print, copy, or link secret-bearing files.",
            }
        )
    for path in (
        repo_root / "data/v2_autonomous_runner/status/latest_status.json",
        repo_root / "data/v2_scheduler/status/latest_status.json",
        repo_root / "data/v2_market_masters/reports/report_latest.json",
        repo_root / "data/v2_telegram_intel/reports/verify_latest.json",
    ):
        payload = _read_json(path, {})
        warnings = payload.get("warnings")
        if isinstance(warnings, list) and warnings:
            risks.append(
                {
                    "risk": f"{_rel(path, repo_root)} has warnings",
                    "severity": "medium",
                    "examples": [str(item) for item in warnings[:8]],
                    "ui_requirement": (
                        "Surface warnings clearly; do not hide them behind green cards."
                    ),
                }
            )
        if payload.get("validation_triggered") is True:
            risks.append(
                {
                    "risk": "A strategy validation flag is true",
                    "severity": "critical",
                    "ui_requirement": "Show validation only when source artifacts prove it.",
                }
            )
    risks.append(
        {
            "risk": (
                "Strategies are research or paper-evidence only unless source artifacts "
                "prove otherwise."
            ),
            "severity": "high",
            "ui_requirement": (
                "Show 'No strategy is validated yet' when validation proof is absent."
            ),
        }
    )
    risks.append(
        {
            "risk": "Public fallback and single-provider evidence may be present.",
            "severity": "medium",
            "ui_requirement": (
                "Label provider quality and stale risk directly on Evidence and Today."
            ),
        }
    )
    return risks


def _write_inventory_docs(*, repo_root: Path, payload: dict[str, Any]) -> None:
    docs = repo_root / "docs/repo_inventory"
    docs.mkdir(parents=True, exist_ok=True)
    _write_text(docs / "dawnstrike_repo_inventory.md", _repo_inventory_md(payload))
    _write_text(docs / "dawnstrike_module_map.md", _module_map_md(payload))
    _write_text(docs / "dawnstrike_cli_map.md", _cli_map_md(payload))
    _write_text(docs / "dawnstrike_artifact_map.md", _artifact_map_md(payload))
    _write_text(docs / "dawnstrike_test_map.md", _test_map_md(payload))
    _write_text(docs / "dawnstrike_data_flow.md", _data_flow_md(payload))
    _write_text(docs / "dawnstrike_current_risks.md", _risks_md(payload))


def _repo_inventory_md(payload: dict[str, Any]) -> str:
    top = payload["top_level"]
    existing = payload["existing_command_center"]
    return "\n".join(
        [
            "# Dawnstrike Repo Inventory",
            "",
            f"- Build ID: `{payload['build_id']}`",
            f"- Created: `{payload['created_at']}`",
            f"- Dirty worktree: `{payload['git']['dirty']}`",
            f"- Top-level directories: `{len(top['directories'])}`",
            f"- Top-level files: `{len(top['files'])}`",
            f"- Existing Command Center pages: `{existing['page_count']}`",
            f"- Existing Command Center QA: `{existing['qa_status']}`",
            "",
            "## Top-Level Directories",
            "",
            _bullet(top["directories"]),
            "",
            "## Major Packages",
            "",
            _table(
                payload["python_packages"],
                ["path", "purpose", "python_file_count", "risk_level", "ui_relevance"],
            ),
            "",
            "## Current Risks",
            "",
            _table(payload["current_risks"], ["severity", "risk", "ui_requirement"]),
            "",
        ]
    )


def _module_map_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Dawnstrike Module Map",
            "",
            "## v2 Modules",
            "",
            _table(
                payload["v2_modules"],
                ["path", "purpose", "has_cli", "cli_command", "risk_level", "ui_relevance"],
            ),
            "",
            "## Primary Symbols",
            "",
            _symbol_sections(payload["v2_modules"]),
            "",
        ]
    )


def _cli_map_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Dawnstrike CLI Map",
            "",
            _table(
                payload["cli_commands"],
                [
                    "module",
                    "exists",
                    "usage",
                    "safe_to_run_daily",
                    "mutates_state",
                    "writes_artifacts",
                    "ui_page",
                ],
            ),
            "",
        ]
    )


def _artifact_map_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Dawnstrike Artifact Map",
            "",
            _table(
                payload["data_artifacts"],
                ["path", "exists", "file_count", "trust_level", "stale_risk", "ui_pages"],
            ),
            "",
        ]
    )


def _test_map_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Dawnstrike Test Map",
            "",
            _table(
                payload["tests"],
                [
                    "path",
                    "area",
                    "test_count",
                    "covers_ui",
                    "covers_safety",
                    "recommended_command_center_x_gap",
                ],
            ),
            "",
        ]
    )


def _data_flow_md(payload: dict[str, Any]) -> str:
    del payload
    return """# Dawnstrike Data Flow

1. DataTruth and AutoData produce provider and canonical data evidence.
2. OMEGA Sentinel consumes evidence, RiskHub, PaperOps, FillTruth, CommitBridge,
   Learning Foundry, and Market Masters artifacts.
3. FillTruth resolves paper evidence quality; CommitBridge decides whether overlay
   evidence becomes official.
4. PaperOps stores pending/open/closed paper state and calendar returns.
5. Learning Foundry and Market Masters create lessons and shadow-only challengers.
6. Autonomous Runner, scheduler scripts, watchdog, and Telegram Intelligence expose run health.
7. Command Center X reads existing artifacts and renders local static HTML only.

Command Center X must not recompute signals, mutate databases, send Telegram,
call providers, or add execution controls.
"""


def _risks_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Dawnstrike Current Risks",
            "",
            _table(payload["current_risks"], ["severity", "risk", "ui_requirement"]),
            "",
            "## Existing UI Limitations",
            "",
            _bullet(payload["existing_command_center"]["limitations"]),
            "",
        ]
    )


def _run(repo_root: Path, args: list[str], *, timeout: int = 30) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


def _symbols(paths: list[Path]) -> list[str]:
    names: list[str] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef):
                if not node.name.startswith("_"):
                    names.append(node.name)
            if len(names) >= 12:
                return names
    return names


def _purpose_from_name(name: str) -> str:
    label = name.replace("_", " ").replace("-", " ")
    purpose = {
        "alpha lab": "historical research and demo artifacts",
        "autodata": "provider-backed and fallback data intake",
        "autonomous runner": "Windows task and zero-touch run health",
        "command center": "existing static local dashboard",
        "command center x": "new task-first static local dashboard",
        "data truth": "canonical data truth and reconciliation",
        "evidence commit": "CommitBridge evidence promotion and blocking",
        "fill truth": "paper fill evidence resolution",
        "learning foundry": "daily lessons and shadow challenger learning",
        "market masters": "public methodology research and shadow challengers",
        "omega sentinel": "daily OMEGA orchestration and status",
        "paper ops": "paper positions, ledgers, and returns",
        "real intraday": "timestamped intraday intake and reconciliation",
        "riskhub": "risk gating and block reasons",
        "telegram intel": "dry-run/env-gated status messaging",
    }.get(label, label)
    return purpose


def _risk_level(path: Path) -> str:
    text = path.as_posix().lower()
    if any(part in text for part in ("risk", "paper", "fill", "commit", "autonomous")):
        return "high"
    if any(part in text for part in ("data", "provider", "telegram", "market")):
        return "medium"
    return "low"


def _ui_relevance(path: Path) -> str:
    text = path.as_posix().lower()
    if "command_center" in text or "dashboard" in text:
        return "primary"
    if any(
        part in text
        for part in (
            "omega",
            "paper",
            "risk",
            "learning",
            "market",
            "telegram",
            "autodata",
            "fill",
            "evidence",
        )
    ):
        return "primary data source"
    return "supporting"


def _guess_artifacts(module_name: str, *, input_side: bool) -> list[str]:
    base = f"data/v2_{module_name}"
    aliases = {
        "omega_sentinel": "data/v2_omega_sentinel",
        "learning_foundry": "data/v2_learning_foundry",
        "market_masters": "data/v2_market_masters",
        "telegram_intel": "data/v2_telegram_intel",
        "autonomous_runner": "data/v2_autonomous_runner",
        "evidence_commit": "data/v2_evidence_commit",
        "fill_truth": "data/v2_fill_truth",
        "paper_ops": "data/v2_paper_ops",
        "data_truth": "data/v2_data_truth",
        "real_intraday": "data/v2_real_intraday",
        "alpha_lab": "data/v2_alpha_lab",
        "command_center": "data/v2_command_center",
    }
    artifact = aliases.get(module_name, base)
    if input_side:
        return ["data/v2_* artifacts", artifact]
    return [artifact]


def _usage(help_text: str) -> str:
    for line in help_text.splitlines():
        if line.startswith("usage:"):
            return line.strip()
    return help_text.splitlines()[0].strip() if help_text.splitlines() else "missing"


def _subcommands(help_text: str) -> list[str]:
    for line in help_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and "}" in stripped:
            return [part.strip() for part in stripped.strip("{}").split(",") if part.strip()]
    return []


def _safe_daily(module: str) -> bool:
    return module.endswith(
        (
            "omega_sentinel",
            "autonomous_runner",
            "telegram_intel",
            "learning_foundry",
            "market_masters",
            "command_center",
            "command_center_x",
        )
    )


def _mutates_state(module: str) -> bool:
    return module.endswith(
        (
            "autodata",
            "fill_truth",
            "evidence_commit",
            "paper_ops",
            "learning_foundry",
            "market_masters",
            "omega_sentinel",
            "autonomous_runner",
        )
    )


def _cli_ui_page(module: str) -> str:
    name = module.rsplit(".", 1)[-1]
    mapping = {
        "omega_sentinel": "today.html",
        "autodata": "evidence.html",
        "fill_truth": "evidence.html",
        "evidence_commit": "evidence.html",
        "paper_ops": "paper_trading.html",
        "learning_foundry": "learning.html",
        "market_masters": "market_masters.html",
        "telegram_intel": "telegram.html",
        "autonomous_runner": "automation.html",
        "command_center": "reports.html",
        "command_center_x": "system.html",
        "cli": "system.html",
    }
    return mapping.get(name, "system.html")


def _matching(files: list[Path], tokens: tuple[str, ...], repo_root: Path) -> list[str]:
    rows = [
        _rel(path, repo_root)
        for path in files
        if any(token in path.name.lower() or token in path.parent.name.lower() for token in tokens)
    ]
    return rows[-16:]


def _trust_level(raw: str) -> str:
    if "paper_ops" in raw or "evidence_commit" in raw:
        return "official paper evidence when CommitBridge marks it official"
    if "autodata" in raw or "data_truth" in raw or "real_intraday" in raw:
        return "provider/canonical evidence with source quality labels"
    if "learning" in raw or "market_masters" in raw or "alpha_lab" in raw:
        return "research/shadow only"
    if "telegram" in raw:
        return "notification draft/send audit only"
    return "artifact evidence"


def _stale_risk(files: list[Path]) -> str:
    if not files:
        return "missing"
    latest = max(path.stat().st_mtime for path in files)
    age_seconds = datetime.now().timestamp() - latest
    if age_seconds > 14 * 24 * 60 * 60:
        return "high"
    if age_seconds > 3 * 24 * 60 * 60:
        return "medium"
    return "low"


def _artifact_ui_pages(raw: str) -> list[str]:
    name = Path(raw).name
    mapping = {
        "v2_autodata": ["evidence.html"],
        "v2_data_truth": ["evidence.html"],
        "v2_real_intraday": ["evidence.html"],
        "v2_fill_truth": ["evidence.html", "paper_trading.html"],
        "v2_evidence_commit": ["evidence.html", "paper_trading.html"],
        "v2_paper_ops": ["paper_trading.html", "strategies.html"],
        "v2_forward_evidence": ["today.html", "strategies.html", "risk.html"],
        "v2_omega_sentinel": ["today.html", "risk.html"],
        "v2_learning_foundry": ["learning.html"],
        "v2_market_masters": ["market_masters.html"],
        "v2_telegram_intel": ["automation.html", "telegram.html"],
        "v2_autonomous_runner": ["automation.html"],
        "v2_scheduler": ["automation.html", "scheduler.html"],
        "v2_command_center": ["reports.html"],
    }
    return mapping.get(name, ["system.html"])


def _test_gap(name: str, text: str) -> str:
    if "command_center_x" in name or "Command Center X" in text:
        return "covers Command Center X"
    if "dashboard" in name or "command_center" in text:
        return "extend with X page/link/QA assertions"
    if "contract" in name or "safety" in text:
        return "reuse safety patterns for X"
    return "not UI-specific"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.is_dir():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "No rows found."
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = [_md_cell(row.get(column, "")) for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value[:8])
    elif isinstance(value, dict):
        value = json.dumps(value, sort_keys=True)[:160]
    return str(value).replace("|", "\\|").replace("\n", " ")[:260]


def _bullet(items: list[Any]) -> str:
    return "\n".join(f"- `{item}`" for item in items) if items else "- None."


def _symbol_sections(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        symbols = row.get("primary_symbols")
        if not symbols:
            continue
        parts.append(f"### {row['path']}\n\n" + _bullet(list(symbols)))
    return "\n\n".join(parts) if parts else "No primary symbols discovered."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
