"""Optional browser-rendered public table extraction."""

from __future__ import annotations

import csv
import importlib.util
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso
from intraday_scanner.providers.public_table_provider import (
    ExtractedTable,
    extract_html_tables,
    normalize_public_table_rows_with_debug,
    select_best_table,
    write_extracted_tables,
)
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    artifact_payload,
    ensure_allowed_url,
    write_json,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

BROWSER_INSTALL_HINT = (
    'BROWSER_EXTRACTOR_NOT_AVAILABLE: run py -m pip install -e ".[browser]" '
    "and py -m playwright install chromium"
)


def ingest_browser_table(
    *,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
    print_rows: bool = False,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    target = source.url or source.fixture_path
    raw_path = output_dir / "rendered_source.html"
    allowed_domains = source.allowed_domains or config.allowed_domains
    if source.url:
        try:
            ensure_allowed_url(source.url, allowed_domains=allowed_domains)
        except Exception as exc:
            return _failure(
                source,
                output_dir,
                "blocked",
                str(exc),
                started_at=started_at,
                target=target,
                store=store if persist else None,
            )

    if source.fixture_path:
        html = Path(source.fixture_path).read_text(encoding="utf-8")
    elif not _playwright_available():
        return _failure(
            source,
            output_dir,
            "browser_extractor_not_available",
            BROWSER_INSTALL_HINT,
            started_at=started_at,
            target=target,
            store=store if persist else None,
        )
    else:
        html = _render_public_page(source, config)

    if _looks_blocked(html):
        return _failure(
            source,
            output_dir,
            "login_required",
            "Public page appears to require login, CAPTCHA, or anti-bot review.",
            started_at=started_at,
            target=target,
            store=store if persist else None,
        )

    if config.save_raw:
        raw_path.write_text(html, encoding="utf-8")
        if persist and store is not None:
            store.persist_raw_source_artifact(
                artifact_payload(
                    run_id=source.name,
                    source=source.name,
                    artifact_kind="browser_html",
                    path=raw_path,
                    content_type="text/html",
                    metadata={"url": source.url, "fixture_path": source.fixture_path},
                )
            )
    tables = extract_browser_tables(html)
    extracted_path = output_dir / "browser_extracted_tables.csv"
    write_extracted_tables(extracted_path, tables)
    best = select_best_table(tables)
    if best is None:
        return _failure(
            source,
            output_dir,
            "no_candidate_table",
            "No visible candidate table or grid was found.",
            started_at=started_at,
            target=target,
            store=store if persist else None,
            rows_extracted=sum(len(table.rows) for table in tables),
        )
    max_rows = int(source.params.get("max_rows") or 0)
    if max_rows > 0:
        best = ExtractedTable(
            index=best.index,
            headers=best.headers,
            rows=best.rows[:max_rows],
            score=best.score,
        )
    rows, warnings, normalization_debug = normalize_public_table_rows_with_debug(
        best,
        source_name=source.name,
        source_url=source.url or source.fixture_path,
        raw_file_path=str(raw_path if config.save_raw else ""),
    )
    for row in rows:
        row["data_source_kind"] = "browser_url"
        row["extraction_mode"] = "browser_url"
        row["source_url"] = source.url or source.fixture_path
        row["preferred_source"] = source.name
        row["source_confidence"] = max(20.0, float(row.get("source_confidence") or 60) - 5)
        warning = str(row.get("coverage_warning") or "")
        row["coverage_warning"] = ";".join(
            part
            for part in [
                warning,
                "browser_rendered_public_table_unverified",
            ]
            if part
        )
    snapshot_path = output_dir / "premarket_snapshot.csv"
    extracted_rows_path = output_dir / "extracted_rows.csv"
    rejected_rows_path = output_dir / "rejected_rows.csv"
    debug_path = output_dir / "normalization_debug.json"
    _write_snapshot(snapshot_path, rows)
    _write_dynamic_csv(extracted_rows_path, normalization_debug["extracted_rows"])
    _write_dynamic_csv(rejected_rows_path, normalization_debug["rejected_rows"])
    write_json(debug_path, normalization_debug)
    summary = {
        "status": "success" if rows else "no_valid_rows",
        "run_id": source.name,
        "source": source.name,
        "source_type": source.type,
        "url": source.url,
        "table_count": len(tables),
        "selected_table_index": best.index,
        "selected_table_score": best.score,
        "rows_extracted": sum(len(table.rows) for table in tables),
        "rows_normalized": len(rows),
        "rows_rejected": len(normalization_debug["rejected_rows"]),
        "rejection_reason_counts": normalization_debug["rejection_reason_counts"],
        "warnings": warnings,
        "paths": {
            "extracted_tables": str(extracted_path),
            "extracted_rows": str(extracted_rows_path),
            "rejected_rows": str(rejected_rows_path),
            "normalization_debug": str(debug_path),
            "premarket_snapshot": str(snapshot_path),
            "raw_source": str(raw_path if config.save_raw else ""),
        },
        "data_source_kind": "browser_url",
        "shadow_mode": True,
        "paid_data": False,
        "coverage_warning": "browser_rendered_public_table_unverified",
        "started_at": started_at,
        "completed_at": utc_now_iso(),
    }
    write_json(output_dir / "browser_extraction_summary.json", summary)
    if persist and store is not None:
        store.persist_web_fetch_result(
            {
                "run_id": source.name,
                "source": source.name,
                "status": summary["status"],
                "row_count": len(rows),
                "artifact_path": str(snapshot_path),
                "failure_reason": "",
                "summary": summary,
            }
        )
        store.record_source_health(
            source.name,
            "ok" if rows else "partial",
            utc_now_iso(),
            f"browser rows={len(rows)}",
            summary,
        )
        store.persist_normalized_source_rows(source.name, source.name, rows)
    if print_rows:
        for row in rows[:10]:
            print(f"{row.get('ticker')} price={row.get('premarket_price')}")
    return summary


def extract_browser_tables(html: str) -> list[ExtractedTable]:
    tables = extract_html_tables(html)
    grid = _GridParser()
    grid.feed(html)
    tables.extend(grid.tables())
    return tables


def browser_extractor_status() -> dict[str, Any]:
    available = _playwright_available()
    return {
        "available": available,
        "status": "available" if available else "missing",
        "install_hint": "" if available else BROWSER_INSTALL_HINT,
    }


def _render_public_page(source: WebSourceConfig, config: WebCollectionConfig) -> str:
    from playwright.sync_api import sync_playwright

    wait_selector = str(source.params.get("wait_selector") or "table, [role='grid']")
    wait_seconds = float(source.params.get("wait_seconds") or 10)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(user_agent=config.user_agent)
        page.goto(
            source.url,
            wait_until="domcontentloaded",
            timeout=int(config.timeout_seconds * 1000),
        )
        try:
            page.wait_for_selector(wait_selector, timeout=int(wait_seconds * 1000))
        except Exception:
            pass
        html = page.content()
        browser.close()
        return str(html)


def _failure(
    source: WebSourceConfig,
    out_dir: Path,
    reason: str,
    failure_reason: str,
    *,
    started_at: str,
    target: str,
    store: SQLiteScanStore | None = None,
    rows_extracted: int = 0,
) -> dict[str, Any]:
    payload = {
        "status": "failed",
        "run_id": source.name,
        "source": source.name,
        "source_type": source.type,
        "url": target,
        "reason": reason,
        "failure_reason": failure_reason,
        "rows_extracted": rows_extracted,
        "rows_normalized": 0,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "data_source_kind": "browser_url",
        "coverage_warning": "browser_rendered_public_table_unverified",
    }
    write_json(out_dir / "browser_failure_report.json", payload)
    if store is not None:
        store.persist_web_fetch_result(
            {
                "run_id": source.name,
                "source": source.name,
                "status": "failed",
                "row_count": 0,
                "artifact_path": "",
                "failure_reason": failure_reason or reason,
                "summary": payload,
            }
        )
        store.record_source_health(source.name, "failed", utc_now_iso(), failure_reason, payload)
    return payload


def _write_snapshot(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_dynamic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["empty"])
        writer.writeheader()
        writer.writerows(rows)


def _playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _looks_blocked(html: str) -> bool:
    lowered = html.lower()
    signals = ["captcha", "sign in", "log in", "login required", "verify you are human"]
    return any(signal in lowered for signal in signals)


class _GridParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_grid = False
        self._in_row = False
        self._in_cell = False
        self._cell = ""
        self._row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): str(value or "").lower() for key, value in attrs}
        role = attrs_dict.get("role", "")
        if role == "grid":
            self._in_grid = True
        elif self._in_grid and role == "row":
            self._in_row = True
            self._row = []
        elif self._in_grid and self._in_row and role in {"cell", "gridcell", "columnheader"}:
            self._in_cell = True
            self._cell = ""

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell += data

    def handle_endtag(self, tag: str) -> None:
        if self._in_cell:
            self._row.append(" ".join(self._cell.split()))
            self._in_cell = False
        elif self._in_row:
            if self._row:
                self.rows.append(self._row)
            self._in_row = False
        elif self._in_grid:
            self._in_grid = False

    def tables(self) -> list[ExtractedTable]:
        if len(self.rows) < 2:
            return []
        headers = [str(cell).strip().lower() for cell in self.rows[0]]
        rows = []
        for raw in self.rows[1:]:
            values = raw + [""] * max(0, len(headers) - len(raw))
            rows.append(dict(zip(headers, values[: len(headers)], strict=False)))
        return [ExtractedTable(index=1000, headers=headers, rows=rows, score=10)]
