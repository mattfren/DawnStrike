"""Filesystem storage helpers for PaperOps v1."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class IncompleteJSONLTailError(ValueError):
    """Raised when a reader observes a partially written final JSONL record."""


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temp_path = _unique_temp_path(path)
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _replace_with_retry(temp_path, path)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
        complete_tail = not lines or lines[-1].endswith(("\n", "\r"))
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped:
                try:
                    loaded = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    if index == len(lines) - 1 and not complete_tail:
                        raise IncompleteJSONLTailError(
                            f"incomplete JSONL tail in {path}"
                        ) from exc
                    raise
                if not isinstance(loaded, dict):
                    raise ValueError(
                        f"JSONL record {index + 1} in {path} must be a JSON object"
                    )
                rows.append(loaded)
    return rows


def append_jsonl_unique(
    path: Path,
    rows: list[dict[str, object]],
    id_field: str,
    *,
    idempotency_ignored_fields: tuple[str, ...] = (),
) -> int:
    ignored_fields = frozenset(idempotency_ignored_fields)
    if id_field in ignored_fields:
        raise ValueError(f"idempotency comparison cannot ignore {id_field}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(jsonl_lock_path(path)):
        _repair_incomplete_jsonl_tail(path)
        existing: dict[str, str] = {}
        for index, row in enumerate(read_jsonl(path), start=1):
            row_id = _required_jsonl_id(
                row,
                id_field,
                location=f"existing JSONL record {index}",
            )
            canonical = _canonical_jsonl_row(row, ignored_fields=ignored_fields)
            existing_prior = existing.get(row_id)
            if existing_prior is not None:
                raise ValueError(
                    f"duplicate {id_field} {row_id!r} already exists in {path}"
                )
            existing[row_id] = canonical

        incoming: dict[str, tuple[str, dict[str, object]]] = {}
        incoming_order: list[str] = []
        for index, row in enumerate(rows, start=1):
            row_id = _required_jsonl_id(
                row,
                id_field,
                location=f"incoming JSONL record {index}",
            )
            canonical = _canonical_jsonl_row(row, ignored_fields=ignored_fields)
            incoming_prior = incoming.get(row_id)
            if incoming_prior is not None:
                if incoming_prior[0] != canonical:
                    raise ValueError(
                        f"conflicting rows reuse {id_field} {row_id!r} in one append"
                    )
                continue
            incoming[row_id] = (canonical, row)
            incoming_order.append(row_id)

        for row_id in incoming_order:
            canonical, _row = incoming[row_id]
            existing_prior = existing.get(row_id)
            if existing_prior is not None and existing_prior != canonical:
                raise ValueError(
                    f"conflicting row reuses existing {id_field} {row_id!r} in {path}"
                )

        appended = 0
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for row_id in incoming_order:
                canonical, row = incoming[row_id]
                if row_id in existing:
                    continue
                json.dump(row, handle, sort_keys=True)
                handle.write("\n")
                existing[row_id] = canonical
                appended += 1
            handle.flush()
            os.fsync(handle.fileno())
        return appended


def _required_jsonl_id(
    row: dict[str, object],
    id_field: str,
    *,
    location: str,
) -> str:
    value = row.get(id_field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} {id_field} must be a nonblank string")
    if value != value.strip():
        raise ValueError(f"{location} {id_field} must not have surrounding whitespace")
    return value


def _canonical_jsonl_row(
    row: dict[str, object],
    *,
    ignored_fields: frozenset[str] = frozenset(),
) -> str:
    try:
        return json.dumps(
            {key: value for key, value in row.items() if key not in ignored_fields},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("JSONL row must contain finite JSON-serializable values") from exc


def _repair_incomplete_jsonl_tail(path: Path) -> None:
    if not path.exists():
        return
    content = path.read_bytes()
    if not content or content.endswith((b"\n", b"\r")):
        return
    boundary = max(content.rfind(b"\n"), content.rfind(b"\r"))
    prefix = content[: boundary + 1]
    tail = content[boundary + 1 :]
    try:
        parsed = json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return
    digest = hashlib.sha256(tail).hexdigest()
    quarantine_path = (
        path.parent
        / "quarantine"
        / f"{path.name}.incomplete_tail.{digest[:16]}.json"
    )
    write_json(
        quarantine_path,
        {
            "byte_length": len(tail),
            "path": path.name,
            "reason": "incomplete_or_malformed_final_jsonl_record",
            "sha256": digest,
            "tail_base64": base64.b64encode(tail).decode("ascii"),
        },
    )
    with path.open("r+b") as handle:
        handle.truncate(len(prefix))
        handle.flush()
        os.fsync(handle.fileno())


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Atomically replace JSONL rows; caller owns the shared ledger lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _unique_temp_path(path)
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            json.dump(row, handle, sort_keys=True)
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _replace_with_retry(temp_path, path)


def jsonl_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Hold a process-safe advisory byte lock, released automatically on exit/crash."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                _lock_handle(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for file lock {path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            _unlock_handle(handle)


def _lock_handle(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock_handle(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _unique_temp_path(path)
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})
        handle.flush()
        os.fsync(handle.fileno())
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


def _unique_temp_path(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple | list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)
