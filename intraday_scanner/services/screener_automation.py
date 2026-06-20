"""Screener inbox automation for Free Shadow Mode."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intraday_scanner.config import load_config
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider, read_snapshot_csv
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.free_shadow_mode import print_upload_prompt
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SCREENER_INBOX = Path("data/inbox/screener")
SCREENER_PROCESSED = Path("data/processed/screener")
SCREENER_FAILED = Path("data/failed/screener")
MANUAL_DATA_DIR = Path("data/manual")
AUTO_SHADOW_OUT = Path("outputs/auto_shadow")
LOG_DIR = Path("logs")
LOG_PATH = LOG_DIR / "screener_automation.log"

SUPPORTED_SUFFIXES = {".csv", ".tsv", ".txt"}

ALIASES = {
    "ticker": ["ticker", "symbol"],
    "company": ["company", "name", "security"],
    "premarket_price": ["premarket_price", "premarket price", "pre-market price", "price", "last"],
    "previous_close": ["previous_close", "previous close", "prev close", "prev_close", "close"],
    "premarket_high": ["premarket_high", "premarket high", "pre-market high", "high"],
    "premarket_low": ["premarket_low", "premarket low", "pre-market low", "low"],
    "premarket_volume": [
        "premarket_volume",
        "premarket volume",
        "pre-market volume",
        "volume",
    ],
    "float_shares": ["float_shares", "float shares", "float"],
    "market_cap": ["market_cap", "market cap"],
    "spread_pct": ["spread_pct", "spread %", "spread"],
    "short_float_pct": ["short_float_pct", "short float", "short float %"],
    "has_news": ["has_news", "has news"],
    "catalyst_headline": ["catalyst_headline", "headline", "news", "catalyst"],
    "catalyst_url": ["catalyst_url", "url", "link", "source url", "source_url"],
    "current_halt": ["current_halt", "halt", "halted"],
    "recent_offering": ["recent_offering", "offering"],
    "reverse_split_90d": ["reverse_split_90d", "reverse split", "reverse_split"],
    "source": ["source"],
    "as_of_timestamp": ["as_of_timestamp", "timestamp", "as_of", "as of", "time"],
}

ENRICHMENT_FIELDS = [
    "float_shares",
    "market_cap",
    "short_float_pct",
    "catalyst_url",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
]


@dataclass(frozen=True)
class NormalizedScreener:
    rows: list[dict[str, Any]]
    raw_rows: list[dict[str, Any]]
    parser: str
    warnings: list[str]


def ensure_screener_directories() -> dict[str, str]:
    paths = {
        "inbox": SCREENER_INBOX,
        "processed": SCREENER_PROCESSED,
        "failed": SCREENER_FAILED,
        "manual": MANUAL_DATA_DIR,
        "output": AUTO_SHADOW_OUT,
        "logs": LOG_DIR,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {key: str(path) for key, path in paths.items()}


def normalize_screener_file(
    *,
    input_path: str | Path,
    out_dir: str | Path,
    ai_normalizer: str = "none",
    store: SQLiteScanStore | None = None,
) -> dict[str, Any]:
    ensure_screener_directories()
    source = Path(input_path)
    file_hash = file_sha256(source)
    imported_at = utc_now_iso()
    try:
        normalized = deterministic_normalize_screener(source, imported_at=imported_at)
    except SnapshotValidationError as exc:
        if ai_normalizer == "none":
            raise
        normalized = _ai_normalize_screener(
            source,
            imported_at=imported_at,
            ai_normalizer=ai_normalizer,
            deterministic_error=str(exc),
        )
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "premarket_snapshot.csv"
    summary_path = output_dir / "normalization_summary.json"
    _write_csv(output_path, normalized.rows, SNAPSHOT_COLUMNS)
    read_snapshot_csv(output_path)
    summary = _normalization_summary(
        source=source,
        output_path=output_path,
        summary_path=summary_path,
        normalized=normalized,
        file_hash=file_hash,
        imported_at=imported_at,
    )
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    if store is not None:
        store.persist_manual_snapshot_upload(
            upload_id=str(uuid.uuid4()),
            created_at=imported_at,
            input_path=str(source),
            output_path=str(output_path),
            raw_rows=normalized.raw_rows,
            normalized_rows=normalized.rows,
            summary=summary,
        )
    return {
        "summary": summary,
        "paths": {"snapshot": output_path, "summary": summary_path},
        "rows": normalized.rows,
    }


def deterministic_normalize_screener(
    input_path: str | Path, *, imported_at: str | None = None
) -> NormalizedScreener:
    source = Path(input_path)
    imported_at = imported_at or utc_now_iso()
    raw_rows = _read_raw_screener(source)
    if not raw_rows:
        raise SnapshotValidationError(f"{source} has no parseable screener rows")
    rows = []
    warnings = []
    for raw in raw_rows:
        row, row_warnings = _normalize_raw_row(raw, source=source, imported_at=imported_at)
        rows.append(row)
        warnings.extend(row_warnings)
    return NormalizedScreener(
        rows=rows,
        raw_rows=raw_rows,
        parser="deterministic",
        warnings=warnings,
    )


def auto_shadow_from_screener(
    *,
    input_path: str | Path,
    db_path: str | Path,
    out_dir: str | Path,
    ai_normalizer: str = "none",
    persist: bool = False,
    print_rows: bool = False,
    move_file: bool = True,
) -> dict[str, Any]:
    ensure_screener_directories()
    source = Path(input_path)
    store = SQLiteScanStore(db_path)
    file_hash = file_sha256(source)
    if store.has_screener_file_hash(file_hash):
        archive = _archive_file(source, SCREENER_PROCESSED, label="duplicate") if move_file else ""
        result = {
            "run_id": str(uuid.uuid4()),
            "status": "skipped_duplicate",
            "input_path": str(source),
            "file_hash": file_hash,
            "started_at": utc_now_iso(),
            "completed_at": utc_now_iso(),
            "raw_archive_path": str(archive),
            "message": "This screener file hash has already been processed.",
        }
        _log_attempt(result)
        return result
    run_id = str(uuid.uuid4())
    started_at = utc_now_iso()
    output_dir = Path(out_dir)
    try:
        normalized = normalize_screener_file(
            input_path=source,
            out_dir=output_dir / "normalized",
            ai_normalizer=ai_normalizer,
            store=store if persist else None,
        )
        snapshot_path = Path(normalized["paths"]["snapshot"])
        config = load_config(
            provider="csv",
            output_dir=output_dir,
            database_path=Path(db_path),
        )
        scan_result = ScanService(CsvSnapshotProvider(snapshot_path), store=store).run(
            config, persist=False
        )
        scan_result.config.update(
            {
                "data_source_kind": "manual",
                "shadow_mode": True,
                "manual_uploaded_data": True,
                "paid_data": False,
                "fixture_only": False,
                "automation_run_id": run_id,
                "source_file_hash": file_hash,
            }
        )
        if persist:
            store.persist_scan_result(scan_result)
        paths = write_scan_outputs(scan_result, output_dir)
        archive = _archive_file(source, SCREENER_PROCESSED, label="processed") if move_file else ""
        completed_at = utc_now_iso()
        summary = {
            "run_id": run_id,
            "status": "success",
            "input_path": str(source),
            "file_hash": file_hash,
            "started_at": started_at,
            "completed_at": completed_at,
            "official_call_timestamp": scan_result.created_at,
            "raw_archive_path": str(archive),
            "normalized_path": str(snapshot_path),
            "out_dir": str(output_dir),
            "scan_run_id": scan_result.run_id,
            "ai_normalizer": ai_normalizer,
            "normalization": normalized["summary"],
            "scan_summary": scan_result.summary(),
            "paths": {key: str(value) for key, value in paths.items()},
            "printed": bool(print_rows),
        }
        _write_run_summary(output_dir, summary)
        if persist:
            store.persist_screener_automation_run(summary)
        _log_attempt(summary)
        return summary
    except Exception as exc:
        archive = _archive_file(source, SCREENER_FAILED, label="failed") if move_file else ""
        completed_at = utc_now_iso()
        failure = {
            "run_id": run_id,
            "status": "failed",
            "input_path": str(source),
            "file_hash": file_hash,
            "started_at": started_at,
            "completed_at": completed_at,
            "raw_archive_path": str(archive),
            "out_dir": str(output_dir),
            "ai_normalizer": ai_normalizer,
            "error": str(exc),
        }
        _write_failure_report(output_dir, failure)
        if persist:
            store.persist_screener_automation_run(failure)
        _log_attempt(failure)
        raise


def watch_screener_inbox(
    *,
    inbox: str | Path,
    db_path: str | Path,
    out_root: str | Path,
    ai_normalizer: str = "none",
    poll_seconds: int = 10,
    max_files: int | None = None,
    max_minutes: float | None = None,
) -> dict[str, Any]:
    ensure_screener_directories()
    inbox_path = Path(inbox)
    inbox_path.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    processed_count = 0
    while True:
        for path in _pending_files(inbox_path):
            out_dir = Path(out_root) / utc_now_iso()[:10] / path.stem
            try:
                result = auto_shadow_from_screener(
                    input_path=path,
                    db_path=db_path,
                    out_dir=out_dir,
                    ai_normalizer=ai_normalizer,
                    persist=True,
                    move_file=True,
                )
            except Exception as exc:
                result = {"status": "failed", "input_path": str(path), "error": str(exc)}
            attempts.append(result)
            processed_count += 1
            if max_files is not None and processed_count >= max_files:
                return _watch_summary(attempts)
        if max_minutes is not None and (time.monotonic() - started) >= max_minutes * 60:
            return _watch_summary(attempts)
        if max_files is None and max_minutes is None:
            time.sleep(max(1, poll_seconds))
            continue
        if not _pending_files(inbox_path):
            time.sleep(max(1, poll_seconds))


def auto_shadow_daily(
    *,
    date: str,
    db_path: str | Path,
    ai_normalizer: str = "none",
    inbox: str | Path = SCREENER_INBOX,
    out_root: str | Path = AUTO_SHADOW_OUT,
) -> dict[str, Any]:
    source = _latest_file_for_date(Path(inbox), date)
    result = auto_shadow_from_screener(
        input_path=source,
        db_path=db_path,
        out_dir=Path(out_root) / date,
        ai_normalizer=ai_normalizer,
        persist=True,
        move_file=True,
    )
    store = SQLiteScanStore(db_path)
    if store.load_manual_outcomes(limit=1):
        from intraday_scanner.services.free_shadow_mode import build_free_shadow_report

        report = build_free_shadow_report(
            store=store,
            out_dir=Path(out_root) / date / "shadow_report",
            persist=True,
        )
        result["shadow_report"] = {key: str(value) for key, value in report["paths"].items()}
    else:
        result["shadow_report_status"] = "skipped_no_manual_outcomes"
    return result


def screener_automation_status(
    *,
    store: SQLiteScanStore | None = None,
    inbox: str | Path = SCREENER_INBOX,
    processed: str | Path = SCREENER_PROCESSED,
    failed: str | Path = SCREENER_FAILED,
) -> dict[str, Any]:
    inbox_path = Path(inbox)
    processed_path = Path(processed)
    failed_path = Path(failed)
    latest_raw = _latest_file(list(_pending_files(inbox_path))) if inbox_path.exists() else None
    runs = store.load_screener_automation_runs(limit=20) if store is not None else []
    latest_run = runs[0] if runs else {}
    return {
        "inbox_path": str(inbox_path),
        "processed_path": str(processed_path),
        "failed_path": str(failed_path),
        "inbox_count": len(list(_pending_files(inbox_path))) if inbox_path.exists() else 0,
        "processed_count": _file_count(processed_path),
        "failed_count": _file_count(failed_path),
        "latest_raw_screener_file": str(latest_raw) if latest_raw else "",
        "latest_normalized_snapshot": str(latest_run.get("normalized_path", "")),
        "normalization_status": str(latest_run.get("status", "")),
        "latest_auto_shadow_run": latest_run,
        "automation_runs": runs,
    }


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    if not source.exists():
        raise DataProviderError(f"Screener file does not exist: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_raw_screener(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SnapshotValidationError(f"Unsupported screener file type: {path.suffix}")
    if suffix == ".txt":
        return _read_text_table(path)
    delimiter = "\t" if suffix == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
            delimiter = dialect.delimiter
        except csv.Error:
            pass
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{path} is empty or missing a header row")
        return list(reader)


def _read_text_table(path: Path) -> list[dict[str, Any]]:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    parsed = [_split_table_line(line) for line in lines if not _is_separator(line)]
    parsed = [row for row in parsed if len(row) > 1]
    if not parsed:
        raise SnapshotValidationError(f"{path} has no parseable table rows")
    headers = parsed[0]
    rows = []
    for values in parsed[1:]:
        if len(values) < len(headers):
            values = values + [""] * (len(headers) - len(values))
        rows.append(dict(zip(headers, values, strict=False)))
    return rows


def _split_table_line(line: str) -> list[str]:
    stripped = line.strip()
    if "|" in stripped:
        return [part.strip() for part in stripped.strip("|").split("|")]
    if "\t" in stripped:
        return [part.strip() for part in stripped.split("\t")]
    return [part.strip() for part in next(csv.reader([stripped]))]


def _is_separator(line: str) -> bool:
    compact = line.replace("|", "").replace(":", "").replace("-", "").strip()
    return compact == ""


def _normalize_raw_row(
    row: dict[str, Any], *, source: Path, imported_at: str
) -> tuple[dict[str, Any], list[str]]:
    warnings = []
    ticker = _text(_alias(row, "ticker")).upper()
    if not ticker:
        raise SnapshotValidationError("ticker/symbol is required")
    price = _required_number(_alias(row, "premarket_price"), "price/last/premarket_price")
    volume = int(_required_number(_alias(row, "premarket_volume"), "premarket_volume/volume"))
    previous_close = _optional_number(_alias(row, "previous_close"))
    high = _optional_number(_alias(row, "premarket_high"))
    low = _optional_number(_alias(row, "premarket_low"))
    if high is None:
        high = price
        warnings.append(f"{ticker}: premarket_high_defaulted_to_price")
    if low is None:
        low = price
        warnings.append(f"{ticker}: premarket_low_defaulted_to_price")
    dollar_volume = price * volume
    gap_pct = 0.0
    if previous_close and previous_close > 0:
        gap_pct = ((price - previous_close) / previous_close) * 100
    elif previous_close in {None, 0}:
        warnings.append(f"{ticker}: previous_close_unknown")
    headline = _text(_alias(row, "catalyst_headline"))
    has_news = _bool_text(_alias(row, "has_news"))
    if not has_news and headline:
        has_news = "true"
    timestamp = _text(_alias(row, "as_of_timestamp")) or imported_at
    source_value = _text(_alias(row, "source")) or "screener_import"
    normalized = {
        "ticker": ticker,
        "company": _text(_alias(row, "company")) or ticker,
        "previous_close": _format_optional(previous_close),
        "premarket_price": _round(price),
        "premarket_high": _round(high),
        "premarket_low": _round(low),
        "premarket_volume": volume,
        "dollar_volume": _round(dollar_volume),
        "gap_pct": _round(gap_pct),
        "float_shares": _format_optional(_optional_number(_alias(row, "float_shares"))),
        "market_cap": _format_optional(_optional_number(_alias(row, "market_cap"))),
        "spread_pct": _format_optional(_optional_number(_alias(row, "spread_pct"))) or 0.0,
        "short_float_pct": _format_optional(_optional_number(_alias(row, "short_float_pct"))),
        "has_news": has_news,
        "catalyst_headline": headline,
        "catalyst_url": _text(_alias(row, "catalyst_url")),
        "current_halt": _bool_text(_alias(row, "current_halt")),
        "recent_offering": _bool_text(_alias(row, "recent_offering")),
        "reverse_split_90d": _bool_text(_alias(row, "reverse_split_90d")),
        "source": source_value,
        "as_of_timestamp": timestamp,
        "data_source_kind": "manual",
        "shadow_mode": "true",
        "paid_data": "false",
        "fixture_only": "false",
        "manual_uploaded_data": "true",
        "raw_file_path": str(source),
        "imported_at": imported_at,
    }
    missing = _missing_fields(normalized)
    normalized["coverage_warning"] = (
        ";".join([*warnings, *missing]) if warnings or missing else "complete"
    )
    normalized["missing_enrichment_count"] = sum(
        1 for field in ENRICHMENT_FIELDS if normalized.get(field) in {None, ""}
    )
    normalized["data_quality_score"] = _data_quality_score(normalized)
    return normalized, warnings


def _ai_normalize_screener(
    source: Path,
    *,
    imported_at: str,
    ai_normalizer: str,
    deterministic_error: str,
) -> NormalizedScreener:
    if ai_normalizer == "codex-cli":
        return _codex_cli_normalize(source, imported_at, deterministic_error)
    if ai_normalizer == "openai-api":
        if not os.environ.get("OPENAI_API_KEY"):
            raise DataProviderError("OPENAI_API_KEY is required for --ai-normalizer openai-api")
        raise DataProviderError(
            "openai-api screener normalization is stubbed in this zero-secrets build; "
            "use --ai-normalizer codex-cli or none."
        )
    raise DataProviderError(f"Unsupported AI normalizer: {ai_normalizer}")


def _codex_cli_normalize(
    source: Path, imported_at: str, deterministic_error: str
) -> NormalizedScreener:
    codex_path = shutil.which("codex")
    if not codex_path:
        raise DataProviderError(
            "Codex CLI is not installed or not on PATH. Install/login to Codex CLI or use "
            "--ai-normalizer none."
        )
    version = subprocess.run(
        [codex_path, "--version"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if version.returncode != 0:
        raise DataProviderError("Codex CLI is installed but not usable. Run `codex login`.")
    prompt = _codex_prompt(source, deterministic_error)
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "codex_normalized.csv"
        completed = subprocess.run(
            [
                codex_path,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "--output-last-message",
                str(output_path),
                "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise DataProviderError(
                "Codex CLI normalization failed. Ensure Codex is logged in. "
                f"Detail: {(completed.stderr or completed.stdout).strip()}"
            )
        if not output_path.exists():
            raise DataProviderError("Codex CLI did not write a normalized CSV response.")
        csv_text = _extract_csv(output_path.read_text(encoding="utf-8"))
        temp_csv = Path(temp_dir) / "candidate.csv"
        temp_csv.write_text(csv_text, encoding="utf-8")
        raw_rows = _read_raw_screener(temp_csv)
    rows = []
    warnings = [f"codex_cli_used_after: {deterministic_error}"]
    for raw in raw_rows:
        row, row_warnings = _normalize_raw_row(raw, source=source, imported_at=imported_at)
        rows.append(row)
        warnings.extend(row_warnings)
    return NormalizedScreener(rows=rows, raw_rows=raw_rows, parser="codex-cli", warnings=warnings)


def _codex_prompt(source: Path, deterministic_error: str) -> str:
    raw_text = source.read_text(encoding="utf-8-sig")
    return (
        f"{print_upload_prompt()}\n\n"
        "The deterministic Dawnstrike parser failed with this error:\n"
        f"{deterministic_error}\n\n"
        "Reformat only the user-provided data below. Do not browse, do not infer prices, "
        "and do not write files.\n\n"
        "<raw_screener>\n"
        f"{raw_text}\n"
        "</raw_screener>\n"
    )


def _extract_csv(text: str) -> str:
    match = re.search(r"```(?:csv)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip() + "\n"
    return text.strip() + "\n"


def _normalization_summary(
    *,
    source: Path,
    output_path: Path,
    summary_path: Path,
    normalized: NormalizedScreener,
    file_hash: str,
    imported_at: str,
) -> dict[str, Any]:
    return {
        "created_at": imported_at,
        "status": "normalized",
        "parser": normalized.parser,
        "input_path": str(source),
        "file_hash": file_hash,
        "output_path": str(output_path),
        "summary_path": str(summary_path),
        "row_count": len(normalized.rows),
        "data_source_kind": "manual",
        "shadow_mode": True,
        "paid_data": False,
        "manual_uploaded_data": True,
        "avg_data_quality_score": _avg(
            [_number(row.get("data_quality_score")) for row in normalized.rows]
        ),
        "missing_enrichment_count": sum(
            int(_number(row.get("missing_enrichment_count"))) for row in normalized.rows
        ),
        "warnings": sorted(set(normalized.warnings)),
    }


def _write_run_summary(out_dir: Path, summary: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )


def _write_failure_report(out_dir: Path, failure: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "error_report.json").write_text(
        json.dumps(failure, indent=2, sort_keys=True), encoding="utf-8"
    )


def _archive_file(path: Path, target_dir: Path, *, label: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return target_dir / f"{label}_missing"
    digest = file_sha256(path)[:12]
    destination = target_dir / f"{path.stem}_{label}_{digest}{path.suffix}"
    counter = 1
    while destination.exists():
        destination = target_dir / f"{path.stem}_{label}_{digest}_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(destination))
    return destination


def _pending_files(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return sorted(
        [
            path
            for path in inbox.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ],
        key=lambda path: path.stat().st_mtime,
    )


def _latest_file_for_date(inbox: Path, date: str) -> Path:
    candidates = [path for path in _pending_files(inbox) if date in path.name]
    if not candidates:
        raise DataProviderError(f"No raw screener file for {date} found in {inbox}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _latest_file(files: list[Path]) -> Path | None:
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _watch_summary(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "complete",
        "attempt_count": len(attempts),
        "success_count": sum(1 for row in attempts if row.get("status") == "success"),
        "failed_count": sum(1 for row in attempts if row.get("status") == "failed"),
        "skipped_count": sum(
            1 for row in attempts if str(row.get("status", "")).startswith("skipped")
        ),
        "attempts": attempts,
    }


def _log_attempt(payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _file_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len([item for item in path.iterdir() if item.is_file()])


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _alias(row: dict[str, Any], canonical: str) -> Any:
    normalized = {_normalize_key(key): value for key, value in row.items()}
    for alias in ALIASES[canonical]:
        value = normalized.get(_normalize_key(alias))
        if value not in {None, ""}:
            return value
    return ""


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _missing_fields(row: dict[str, Any]) -> list[str]:
    return [f"{field}_unknown" for field in ENRICHMENT_FIELDS if row.get(field) in {None, ""}]


def _data_quality_score(row: dict[str, Any]) -> float:
    checks = [
        row.get("previous_close") not in {None, ""},
        _number(row.get("premarket_price")) > 0,
        _number(row.get("premarket_volume")) > 0,
        _number(row.get("dollar_volume")) > 0,
        row.get("float_shares") not in {None, ""},
        row.get("market_cap") not in {None, ""},
        row.get("short_float_pct") not in {None, ""},
        bool(row.get("as_of_timestamp")),
    ]
    return round((sum(1 for passed in checks if passed) / len(checks)) * 100, 2)


def _required_number(value: Any, label: str) -> float:
    number = _optional_number(value)
    if number is None:
        raise SnapshotValidationError(f"{label} is required")
    return number


def _optional_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    multiplier = 1.0
    if text.lower().endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.lower().endswith("b"):
        multiplier = 1_000_000_000
        text = text[:-1]
    if not text:
        return None
    try:
        return float(text) * multiplier
    except ValueError as exc:
        raise SnapshotValidationError(f"Expected numeric screener value, got {value!r}") from exc


def _format_optional(value: float | None) -> str:
    return "" if value is None else str(_round(value))


def _round(value: float) -> float:
    return round(float(value), 6)


def _bool_text(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "t", "1", "yes", "y", "news"}:
        return "true"
    if normalized in {"false", "f", "0", "no", "n"}:
        return "false"
    return ""


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _number(value: Any) -> float:
    try:
        if value in {None, ""}:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()
