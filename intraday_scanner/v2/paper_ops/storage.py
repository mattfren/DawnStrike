"""Filesystem storage helpers for PaperOps v1."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    _replace_with_retry(temp_path, path)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                loaded = json.loads(stripped)
                if isinstance(loaded, dict):
                    rows.append(loaded)
    return rows


def append_jsonl_unique(path: Path, rows: list[dict[str, object]], id_field: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {str(row.get(id_field)) for row in read_jsonl(path)}
    appended = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            row_id = str(row.get(id_field))
            if row_id in existing:
                continue
            json.dump(row, handle, sort_keys=True)
            handle.write("\n")
            existing.add(row_id)
            appended += 1
    return appended


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})
    _replace_with_retry(temp_path, path)


def upsert_rows(
    path: Path,
    rows: list[dict[str, object]],
    key_fields: tuple[str, ...],
    fieldnames: tuple[str, ...],
) -> None:
    existing_rows: list[dict[str, object]] = []
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            existing_rows = list(csv.DictReader(handle))
    by_key = {_row_key(row, key_fields): row for row in existing_rows}
    for row in rows:
        by_key[_row_key(row, key_fields)] = row
    write_csv(path, list(by_key.values()), fieldnames)


def _row_key(row: dict[str, object], fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(field, "")) for field in fields)


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple | list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)
