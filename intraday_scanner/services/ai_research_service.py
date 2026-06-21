"""Optional constrained AI/rule research summarization."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

from intraday_scanner.ai.research_prompts import RESEARCH_OUTPUT_COLUMNS, RESEARCH_SYSTEM_PROMPT
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import utc_now_iso
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

AI_MODES = {"none", "codex-cli", "openai-api"}


def run_ai_research(
    *,
    rows: list[dict[str, Any]],
    mode: str = "none",
    store: SQLiteScanStore | None = None,
    persist: bool = False,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    if mode not in AI_MODES:
        raise DataProviderError(f"Unsupported ai mode: {mode}")
    started_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    warnings: list[dict[str, Any]] = []
    if mode == "none":
        outputs = [_rule_output(row) for row in rows]
        status = "rule_only"
    elif mode == "codex-cli":
        outputs = _run_codex_cli(rows)
        status = "success"
    else:
        outputs = _run_openai_api(rows)
        status = "success"
    for row in outputs:
        if row.get("data_warnings"):
            warnings.append(
                {
                    "run_id": run_id,
                    "ticker": row.get("ticker", ""),
                    "warning": str(row.get("data_warnings") or ""),
                    "created_at": utc_now_iso(),
                }
            )
    run = {
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "row_count": len(rows),
        "output_count": len(outputs),
        "allowed_uses": [
            "summarize supplied catalyst text",
            "classify supplied headlines and filings",
            "generate concise research notification copy",
        ],
        "forbidden_uses": [
            "invent market data",
            "claim certainty",
            "overwrite raw data",
            "give buy or sell advice",
        ],
    }
    result = {"run": run, "outputs": outputs, "warnings": warnings}
    if out_dir is not None:
        _write_outputs(Path(out_dir), result)
    if persist and store is not None:
        store.persist_ai_research(run, outputs, warnings)
    return result


def validate_ai_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None:
        raise DataProviderError("AI output is missing a CSV header.")
    missing = [column for column in RESEARCH_OUTPUT_COLUMNS if column not in reader.fieldnames]
    if missing:
        raise DataProviderError("AI output missing required column(s): " + ", ".join(missing))
    rows = [dict(row) for row in reader]
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            raise DataProviderError("AI output row is missing ticker.")
        row["ticker"] = ticker
    return rows


def _rule_output(row: dict[str, Any]) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "").upper()
    text = " ".join(
        str(row.get(key) or "")
        for key in ("catalyst_headline", "risk_flags", "coverage_warning", "sec_risk_events")
    ).lower()
    classification = "neutral"
    risk_label = ""
    if any(term in text for term in ("offering", "shelf", "warrant", "dilution")):
        classification = "bearish"
        risk_label = "dilution_risk"
    elif any(term in text for term in ("halt", "nasdaq_halt")):
        classification = "bearish"
        risk_label = "halt_risk"
    elif any(term in text for term in ("contract", "fda", "earnings", "merger", "approval")):
        classification = "bullish"
        risk_label = "catalyst_confirmed"
    warnings = []
    if not row.get("catalyst_headline"):
        warnings.append("no supplied catalyst headline")
    if "unverified" in text:
        warnings.append("some enrichment is unverified")
    return {
        "ticker": ticker,
        "classification": classification,
        "risk_label": risk_label,
        "catalyst_summary": str(row.get("catalyst_headline") or "No supplied catalyst text."),
        "data_warnings": "; ".join(warnings),
        "mode": "none",
    }


def _run_codex_cli(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codex = shutil.which("codex")
    if not codex:
        raise DataProviderError("codex-cli mode requires the codex executable to be installed.")
    prompt = _prompt_for_rows(rows)
    command = [codex, "exec", "--full-auto", prompt]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DataProviderError(f"codex-cli research failed: {exc}") from exc
    return [dict(row) for row in validate_ai_csv(completed.stdout)]


def _run_openai_api(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise DataProviderError("openai-api mode requires OPENAI_API_KEY.")
    raise DataProviderError("openai-api mode is not executed in offline/local tests.")


def _prompt_for_rows(rows: list[dict[str, Any]]) -> str:
    columns = ",".join(RESEARCH_OUTPUT_COLUMNS)
    return (
        f"{RESEARCH_SYSTEM_PROMPT}\n"
        f"Return only CSV with columns: {columns}\n"
        f"Rows: {rows[:20]}"
    )


def _write_outputs(out_dir: Path, result: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "ai_research_outputs.csv"
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESEARCH_OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["outputs"])
