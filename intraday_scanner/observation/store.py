"""Small append-only store for raw observation sidecar artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from uuid import uuid4


class ObservationStoreError(RuntimeError):
    pass


class ObservationLockError(ObservationStoreError):
    pass


class ObservationLock(AbstractContextManager["ObservationLock"]):
    """Fail-closed process lock; stale locks require explicit operator review."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid4().hex

    def __enter__(self) -> ObservationLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "token": self.token}
        try:
            handle = self.path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise ObservationLockError(f"observation lock is already held: {self.path}") from exc
        try:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        finally:
            handle.close()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if value.get("token") == self.token:
            self.path.unlink(missing_ok=True)


class ObservationStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "raw-events.jsonl"
        self.census_path = self.root / "universe-census.jsonl"
        self.receipts_path = self.root / "receipts.jsonl"
        self.corrections_path = self.root / "corrections.jsonl"
        self.cursor_path = self.root / "cursor.json"
        self.lock_path = self.root / ".observer.lock"

    def append(self, path: Path, value: dict[str, Any]) -> None:
        data = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def append_event_once(self, value: dict[str, Any]) -> bool:
        event_id = str(value.get("event_id") or "")
        if not event_id:
            raise ObservationStoreError("raw event requires a stable event_id")
        rows = self.read_jsonl(self.events_path)
        existing = {str(row.get("event_id")) for row in rows}
        if event_id in existing:
            return False
        logical_identity = (
            value.get("session_id"),
            value.get("scope"),
            value.get("symbol"),
            value.get("source"),
            value.get("event_time"),
        )
        for prior in rows:
            prior_identity = tuple(prior.get(key) for key in (
                "session_id", "scope", "symbol", "source", "event_time"
            ))
            if prior_identity == logical_identity:
                self.append(
                    self.corrections_path,
                    {
                        "schema_version": "dawnstrike.observation.correction.v1",
                        "prior_event_id": prior.get("event_id"),
                        "replacement_event_id": event_id,
                        "reason": "same_source_window_changed_payload",
                    },
                )
                break
        self.append(self.events_path, value)
        return True

    def write_cursor(self, value: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".cursor.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.cursor_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def read_cursor(self) -> dict[str, Any] | None:
        if not self.cursor_path.exists():
            return None
        try:
            value = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ObservationStoreError("cursor is unreadable and requires recovery") from exc
        if not isinstance(value, dict):
            raise ObservationStoreError("cursor must be an object")
        return value

    def read_jsonl(self, path: Path, *, recover_trailing: bool = True) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        last_good = 0
        with path.open("rb") as handle:
            while True:
                raw = handle.readline()
                if not raw:
                    break
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    if recover_trailing and handle.tell() == path.stat().st_size:
                        with path.open("r+b") as repair:
                            repair.truncate(last_good)
                        break
                    raise ObservationStoreError(f"corrupt append-only file: {path}") from exc
                if not isinstance(value, dict):
                    raise ObservationStoreError(f"append-only row is not an object: {path}")
                rows.append(value)
                last_good = handle.tell()
        return rows

    def last_cursor(self) -> dict[str, Any] | None:
        return self.read_cursor()
