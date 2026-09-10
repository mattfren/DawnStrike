"""Bounded delayed historical SIP bar production for OPS05.

This module is an observation-only adapter.  It can consume the existing
read-only Alpaca client, but never performs a network request unless the CLI is
explicitly invoked with ``--execute``.  The default and all tests use a fake
page provider so the source contract can be independently checked first.
"""

from __future__ import annotations

import copy
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import random
import time as wall_clock
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session
from intraday_scanner.providers.base import IntradayPage

PANEL_SYMBOLS = ("SPY", "IWM", "QQQ", "DIA", "TLT")
ORIGINAL_SCOPE = "original_small_cap_gap"
REFERENCE_SCOPE = "liquid_reference_panel"
SAMPLE_SEED = 27039
MAX_MOVER_SYMBOLS = 12
MAX_PAGES = 100
MAX_RETRIES = 3
MAX_EVENTS = 10_000
MAX_BYTES = 64 * 1024 * 1024
MAX_RSS_BYTES = 256 * 1024 * 1024
MAX_WALL_SECONDS = 1_800
WINDOW_SCHEMA = "dawnstrike.ops05.historical_window.v1"
RECEIPT_SCHEMA = "dawnstrike.ops05.historical_bars_receipt.v1"


def _atomic_write(path: Path, data: bytes) -> None:
    """Publish one complete artifact without exposing a torn JSON/page file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n")


class _DurableCapture:
    """Small append/checkpoint journal for one authenticated OPS05 capture.

    The ledger lives beside the requested output root and is keyed by the
    source/window identity, so moving output files cannot reset capture caps.
    """

    schema = "dawnstrike.ops05.durable_capture.v1"

    def __init__(
        self, *, root: Path, identity: dict[str, Any], resume_across_roots: bool = False
    ) -> None:
        self.root = root
        self.identity = identity
        self.fingerprint = _sha256(identity)
        ledger_parent = root.parent / ".ops05-capture-ledger"
        root_key = (
            "shared"
            if resume_across_roots
            else hashlib.sha256(str(root).encode()).hexdigest()
        )
        self.ledger = ledger_parent / self.fingerprint / root_key
        self.state_path = self.ledger / "state.json"
        self.index_path = ledger_parent / "index.json"
        self.lock_path = self.ledger / "capture.lock"
        self.page_dir = self.ledger / "pages"
        self.state: dict[str, Any] = {}
        self._held = False

    def _identity_matches(self, state: Mapping[str, Any]) -> bool:
        return (
            state.get("identity") == self.identity
            and state.get("fingerprint") == self.fingerprint
        )

    def existing_receipt(self) -> dict[str, Any] | None:
        path = self.root / "receipt.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise Ops05Error("existing OPS05 receipt is unreadable") from exc
        if not isinstance(value, dict):
            raise Ops05Error("existing OPS05 receipt is not an object")
        lineage = value.get("source_lineage") or {}
        if (
            value.get("market_date") != self.identity["market_date"]
            or lineage.get("source_config_sha256") != self.identity["source_config_sha256"]
            or lineage.get("capture_receipt_sha256") != self.identity["capture_receipt_sha256"]
        ):
            raise Ops05Error("immutable OPS05 output conflicts with capture identity")
        raw_path = self.root / "raw-bars.jsonl"
        if raw_path.is_file():
            rows = [
                json.loads(line)
                for line in raw_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            if _sha256(rows) != value.get("raw_event_stream_sha256"):
                raise Ops05Error("immutable OPS05 raw artifact changed")
        binding_path = self.root / "capture-binding.json"
        if binding_path.is_file():
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            declared = binding.pop("binding_sha256", None)
            if declared != _sha256(binding):
                raise Ops05Error("immutable OPS05 binding changed")
        return value

    def __enter__(self) -> _DurableCapture:
        self.ledger.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        index: dict[str, Any] = {}
        if self.index_path.is_file():
            try:
                index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise Ops05Error("OPS05 capture identity index is unreadable") from exc
        index_key = _sha256({
            "market_date": self.identity["market_date"],
            "capture_receipt_sha256": self.identity["capture_receipt_sha256"],
            "census_sha256": self.identity["census_sha256"],
            "provider": self.identity["provider"],
            "feed": self.identity["feed"],
        })
        prior_fingerprint = index.get(index_key)
        if prior_fingerprint and prior_fingerprint != self.fingerprint:
            raise Ops05Error("OPS05 source/config/window identity conflicts with prior capture")
        index[index_key] = self.fingerprint
        _atomic_json(self.index_path, index)
        if self.lock_path.exists():
            try:
                owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise Ops05Error("OPS05 capture lock is unreadable; refusing takeover") from exc
            pid = owner.get("pid") if isinstance(owner, dict) else None
            if pid == os.getpid() or _pid_exists(pid):
                raise Ops05Error("OPS05 capture already has a live owner")
            raise Ops05Error("OPS05 stale capture owner requires explicit reconciliation")
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise Ops05Error("OPS05 capture lock acquisition raced") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": _now()}, handle, sort_keys=True)
        self._held = True
        if self.state_path.is_file():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise Ops05Error("OPS05 durable capture state is unreadable") from exc
            if not isinstance(self.state, dict) or not self._identity_matches(self.state):
                raise Ops05Error("OPS05 durable capture identity changed")
            changed = False
            for key, record in (self.state.get("pages") or {}).items():
                if not isinstance(record, dict):
                    raise Ops05Error(f"OPS05 durable page record is invalid: {key}")
                page_path = self.ledger / str(record.get("page_path") or "")
                try:
                    page_value = json.loads(page_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise Ops05Error("OPS05 durable page body is unreadable") from exc
                for field in ("item_count", "observed_item_bytes"):
                    if field not in record:
                        record[field] = page_value.get(field, 0)
                        changed = True
            if changed:
                self.state["event_count"] = sum(
                    int(item.get("item_count", 0)) for item in self.state["pages"].values()
                )
                self.state["raw_payload_bytes"] = sum(
                    int(item.get("observed_item_bytes", 0))
                    for item in self.state["pages"].values()
                )
                self._save()
        else:
            self.state = {
                "schema_version": self.schema,
                "fingerprint": self.fingerprint,
                "identity": self.identity,
                "status": "RUNNING",
                "started_at": _now(),
                "original_timestamps": {},
                "pages": {},
                "journal": [],
                "page_count": 0,
                "attempt_count": 0,
                "event_count": 0,
                "raw_payload_bytes": 0,
                "wall_seconds_observed": 0.0,
            }
            self._save()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._held:
            self.lock_path.unlink(missing_ok=True)
            self._held = False

    def _save(self) -> None:
        _atomic_json(self.state_path, self.state)
        _atomic_json(self.root / "capture-state.json", self.state)
        ledger_bytes = self.ledger_bytes()
        if ledger_bytes > MAX_BYTES:
            raise Ops05Error("OPS05 durable journal exceeds the 64 MiB bound")

    def ledger_bytes(self) -> int:
        if not self.state_path.is_file():
            return 0
        return self.state_path.stat().st_size + sum(
            path.stat().st_size for path in self.page_dir.glob("*.json") if path.is_file()
        )

    def check_budget(self, *, events: int = 0, payload_bytes: int = 0) -> None:
        if int(self.state.get("page_count", 0)) >= MAX_PAGES:
            raise Ops05Error("provider page cap exceeded")
        if int(self.state.get("event_count", 0)) + events > MAX_EVENTS:
            raise Ops05Error("derived event cap exceeded")
        if int(self.state.get("raw_payload_bytes", 0)) + payload_bytes > MAX_BYTES:
            raise Ops05Error("raw provider payload exceeds the 64 MiB bound")
        if float(self.state.get("wall_seconds_observed", 0.0)) > MAX_WALL_SECONDS:
            raise Ops05Error("OPS05 wall-time cap exceeded")

    def check_runtime(self, started_at: float) -> None:
        elapsed = wall_clock.monotonic() - started_at
        if float(self.state.get("wall_seconds_observed", 0.0)) + elapsed > MAX_WALL_SECONDS:
            raise Ops05Error("OPS05 cumulative wall-time cap exceeded")
        _check_runtime(started_at)

    def note_runtime(self, started_at: float) -> None:
        self.state["wall_seconds_observed"] = round(
            float(self.state.get("wall_seconds_observed", 0.0))
            + max(0.0, wall_clock.monotonic() - started_at),
            6,
        )

    def attempt_started(self, key: str, attempt: int, request: dict[str, Any]) -> None:
        self.state["journal"].append({
            "kind": "request_attempt_started",
            "key": key,
            "attempt": attempt,
            "request": request,
            "at": _now(),
        })
        self.state["attempt_count"] = int(self.state.get("attempt_count", 0)) + 1
        self._save()

    def attempt_failed(self, key: str, attempt: int, error: str) -> None:
        self.state["journal"].append({
            "kind": "request_attempt_uncertain",
            "key": key,
            "attempt": attempt,
            "error": error,
            "at": _now(),
        })
        self._save()

    def page(self, key: str) -> dict[str, Any] | None:
        record = (self.state.get("pages") or {}).get(key)
        if not isinstance(record, dict):
            return None
        path = self.ledger / str(record.get("page_path") or "")
        if not path.is_file():
            raise Ops05Error("OPS05 durable page body is missing")
        try:
            page = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise Ops05Error("OPS05 durable page body is unreadable") from exc
        if not isinstance(page, dict) or page.get("raw_payload_hash_sha256") != record.get(
            "raw_payload_hash_sha256"
        ):
            raise Ops05Error("OPS05 durable page binding changed")
        if _sha256(page.get("raw_payload_items", [])) != record.get("raw_payload_hash_sha256"):
            raise Ops05Error("OPS05 durable page payload hash changed")
        return page

    def commit_page(self, key: str, page: dict[str, Any], *, page_bytes: int) -> None:
        self.check_budget(events=int(page.get("item_count", 0)), payload_bytes=page_bytes)
        filename = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        page_path = self.page_dir / filename
        _atomic_json(page_path, page)
        record = {
            "page_path": f"pages/{filename}",
            "page_number": page["page_number"],
            "endpoint": page["endpoint"],
            "cursor_in": page.get("cursor_in"),
            "cursor_out": page.get("cursor_out"),
            "raw_payload_hash_sha256": page["raw_payload_hash_sha256"],
            "request_id": page.get("request_id"),
            "item_count": page.get("item_count", 0),
            "observed_item_bytes": page.get("observed_item_bytes", 0),
        }
        self.state.setdefault("pages", {})[key] = record
        self.state["page_count"] = len(self.state["pages"])
        self.state["event_count"] = sum(
            int(item.get("item_count", 0)) for item in self.state["pages"].values()
        )
        self.state["raw_payload_bytes"] = sum(
            int(item.get("observed_item_bytes", 0))
            for item in self.state["pages"].values()
        )
        self.state["journal"].append({"kind": "page_committed", "key": key, "at": _now()})
        self._save()

    def fail(self, phase: str, error: BaseException) -> None:
        self.state["status"] = "PARTIAL"
        self.state["failure"] = {
            "phase": phase,
            "type": type(error).__name__,
            "message": str(error),
            "at": _now(),
        }
        self._save()
        _atomic_json(self.root / "partial-receipt.json", {
            "schema_version": "dawnstrike.ops05.partial_capture_receipt.v1",
            "status": "PARTIAL",
            "identity": self.identity,
            "durable_page_count": len(self.state.get("pages", {})),
            "derived_bar_count": self.state.get("derived_bar_count", 0),
            "derived_boundary_count": self.state.get("derived_boundary_count", 0),
            "derived_raw_stream_sha256": self.state.get("derived_raw_stream_sha256"),
            "failure": self.state["failure"],
            "prior_pages_retained": True,
        })

    def checkpoint_derived(
        self, bars: Sequence[Mapping[str, Any]], boundary_events: Sequence[Mapping[str, Any]]
    ) -> None:
        raw = "\n".join(json.dumps(row, sort_keys=True) for row in bars) + ("\n" if bars else "")
        boundary = "\n".join(
            json.dumps(row, sort_keys=True) for row in boundary_events
        ) + ("\n" if boundary_events else "")
        _atomic_write(self.root / "partial" / "raw-bars.jsonl", raw.encode("utf-8"))
        _atomic_write(self.root / "partial" / "boundary-events.jsonl", boundary.encode("utf-8"))
        self.state["derived_bar_count"] = len(bars)
        self.state["derived_boundary_count"] = len(boundary_events)
        self.state["derived_raw_stream_sha256"] = _sha256(list(bars))
        self.state["status"] = "RUNNING"
        self._save()

    def complete(self) -> None:
        self.state["status"] = "CAPTURED"
        self._save()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _pid_exists(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name == "nt":
        if value == os.getpid():
            return True
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, value)
        if not handle:
            return False
        kernel.CloseHandle(handle)
        return True
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


class Ops05Error(ValueError):
    """The bounded historical source contract is invalid."""


class HistoricalBarsProvider(Protocol):
    provider_name: str
    feed: str

    def get_bars_page(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        config: Any,
        *,
        page_token: str | None = None,
    ) -> IntradayPage: ...

    def get_corporate_actions_page(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        config: Any,
        *,
        page_token: str | None = None,
    ) -> IntradayPage: ...


@dataclass(frozen=True)
class Window:
    name: str
    start: datetime
    end: datetime
    market_date: str
    session_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "market_date": self.market_date,
            "session_id": self.session_id,
            "start_utc": self.start.isoformat().replace("+00:00", "Z"),
            "end_utc": self.end.isoformat().replace("+00:00", "Z"),
            "membership": "half_open_derived_from_provider_inclusive_boundary",
        }


def _session_window(market_date: date) -> tuple[datetime, datetime, str]:
    decision = market_session(market_date)
    if not decision.is_trading_day or not decision.open_time_et or not decision.close_time_et:
        raise Ops05Error(f"{market_date.isoformat()} is not a trading session")
    start = datetime.combine(
        market_date, time.fromisoformat(decision.open_time_et), tzinfo=MARKET_TIMEZONE
    ).astimezone(UTC)
    end = datetime.combine(
        market_date, time.fromisoformat(decision.close_time_et), tzinfo=MARKET_TIMEZONE
    ).astimezone(UTC)
    return start, end, f"XNYS:{market_date.isoformat()}:regular"


def _previous_trading_day(market_date: date) -> date:
    candidate = market_date - timedelta(days=1)
    while not market_session(candidate).is_trading_day:
        candidate -= timedelta(days=1)
    return candidate


def build_windows(market_date: str) -> dict[str, Window]:
    """Build full-session, prior-close, and bounded corporate-action windows."""

    current = date.fromisoformat(market_date)
    start, end, session_id = _session_window(current)
    prior = _previous_trading_day(current)
    prior_start, prior_end, prior_session_id = _session_window(prior)
    return {
        "full_session": Window("full_session", start, end, current.isoformat(), session_id),
        "prior_close": Window(
            "prior_close",
            prior_end - timedelta(minutes=30),
            prior_end,
            prior.isoformat(),
            prior_session_id,
        ),
        "corporate_actions": Window(
            "corporate_actions",
            prior_start.replace(hour=0, minute=0, second=0, microsecond=0),
            end.replace(hour=23, minute=59, second=59, microsecond=0),
            current.isoformat(),
            session_id,
        ),
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _parse_timestamp(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise Ops05Error(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise Ops05Error(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _allocate_quotas(
    rows_by_membership: Mapping[str, list[dict[str, Any]]], limit: int
) -> dict[str, int]:
    order = ("selected", "rejected", "unselected", "missing_input")
    present = [key for key in order if rows_by_membership.get(key)]
    target = min(limit, sum(len(rows_by_membership[key]) for key in present))
    quotas = {key: 0 for key in rows_by_membership}
    base = target // len(order) if order else 0
    for key in order:
        quotas[key] = min(len(rows_by_membership.get(key, [])), base)
    remaining = target - sum(quotas.values())
    while remaining:
        progressed = False
        for key in order:
            if quotas[key] < len(rows_by_membership.get(key, [])):
                quotas[key] += 1
                remaining -= 1
                progressed = True
                if not remaining:
                    break
        if not progressed:
            break
    return quotas


def select_movers(
    census: Sequence[Mapping[str, Any]], *, seed: int = SAMPLE_SEED
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return all census rows with deterministic sample annotations.

    The complete census remains in the returned rows.  Only rows marked
    ``sampled_for_bars`` become provider symbols.
    """

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    membership_order = ("selected", "rejected", "unselected", "missing_input")
    by_membership: dict[str, list[dict[str, Any]]] = {key: [] for key in membership_order}
    for raw in census:
        row = dict(raw)
        symbol = str(row.get("symbol") or "").strip().upper()
        membership = str(row.get("membership") or "").strip().lower()
        if not symbol or symbol in seen:
            raise Ops05Error("original census must contain unique non-empty symbols")
        if membership not in by_membership:
            raise Ops05Error(
                "original census membership must be selected/rejected/unselected/missing_input"
            )
        seen.add(symbol)
        row["symbol"] = symbol
        row["membership"] = membership
        row["sampled_for_bars"] = False
        row["inclusion_probability"] = 0.0
        row["missingness"] = "not_sampled_by_seed"
        by_membership[membership].append(row)
        rows.append(row)
    quotas = _allocate_quotas(by_membership, MAX_MOVER_SYMBOLS)
    rng = random.Random(seed)
    selected: list[str] = []
    for membership in membership_order:
        pool = sorted(by_membership[membership], key=lambda row: row["symbol"])
        rng.shuffle(pool)
        chosen = sorted(pool[: quotas.get(membership, 0)], key=lambda row: row["symbol"])
        probability = min(1.0, len(chosen) / len(pool)) if pool else 0.0
        for row in chosen:
            row["sampled_for_bars"] = True
            row["inclusion_probability"] = probability
            row["missingness"] = "pending_source_observation"
            selected.append(row["symbol"])
    return rows, {
        "seed": seed,
        "requested_limit": MAX_MOVER_SYMBOLS,
        "sampled_symbols": sorted(selected),
        "quotas": quotas,
        "sampling_is_outcome_independent": True,
        "full_census_retained": True,
    }


def _bar_value(item: Mapping[str, Any], key: str, short: str) -> Any:
    return item.get(short, item.get(key))


def _normalize_bar(
    item: Mapping[str, Any],
    *,
    window: Window,
    source_hash: str,
    source_identity: str,
) -> dict[str, Any]:
    symbol = str(item.get("symbol") or item.get("S") or "").upper()
    event_time = _parse_timestamp(_bar_value(item, "timestamp", "t"), label="bar timestamp")
    payload = {
        "symbol": symbol,
        "timestamp": event_time.isoformat().replace("+00:00", "Z"),
        "open": _bar_value(item, "open", "o"),
        "high": _bar_value(item, "high", "h"),
        "low": _bar_value(item, "low", "l"),
        "close": _bar_value(item, "close", "c"),
        "volume": _bar_value(item, "volume", "v"),
    }
    if not symbol or any(
        payload[key] is None for key in ("open", "high", "low", "close", "volume")
    ):
        raise Ops05Error("bar is missing symbol, timestamp, or OHLCV field")
    return {
        "schema_version": "dawnstrike.ops05.bar.v1",
        "event_id": _sha256({"window": window.name, **payload}),
        "source": source_identity,
        "source_artifact_hash_sha256": source_hash,
        "source_page_number": int(item["__ops05_page_number"]),
        "source_page_row_index": int(item["__ops05_page_row_index"]),
        "source_page_hash_sha256": str(item["__ops05_page_hash_sha256"]),
        "source_window": window.name,
        "source_payload_sha256": _sha256(
            {key: value for key, value in item.items() if not key.startswith("__ops05_")}
        ),
        "window": window.name,
        "session_id": window.session_id,
        "symbol": symbol,
        "event_time": payload["timestamp"],
        "available_at": payload["timestamp"],
        "payload": payload,
        "timing_class": "delayed_historical",
        "decision_eligible": False,
    }


def _fetch_pages(
    provider: HistoricalBarsProvider,
    method: str,
    symbols: Sequence[str],
    start: str,
    end: str,
    config: Any,
    *,
    page_counter: list[int],
    event_counter: list[int],
    byte_counter: list[int],
    started_at: float,
    expected_endpoint: str,
    logical_key_prefix: str = "capture",
    durable: _DurableCapture | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    page_receipts: list[dict[str, Any]] = []
    token: str | None = None
    call_config = copy.copy(config)
    if hasattr(call_config, "request_retries"):
        call_config.request_retries = 1
    for page_number in range(MAX_PAGES):
        if durable is not None:
            durable.check_runtime(started_at)
        else:
            _check_runtime(started_at)
        if page_counter[0] >= MAX_PAGES:
            raise Ops05Error("provider page cap exceeded")
        page_key = f"{logical_key_prefix}:{expected_endpoint}:{page_number}"
        resumed = durable.page(page_key) if durable is not None else None
        if resumed is not None:
            page_items = [dict(item) for item in resumed.get("raw_payload_items", [])]
            page_receipts.append(dict(resumed))
            page_counter[0] += 1
            event_counter[0] += len(page_items)
            byte_counter[0] += int(resumed.get("observed_item_bytes", 0))
            items.extend(
                {
                    **item,
                    "__ops05_page_number": page_number,
                    "__ops05_page_row_index": row_index,
                    "__ops05_page_hash_sha256": resumed["raw_payload_hash_sha256"],
                }
                for row_index, item in enumerate(page_items)
            )
            if not resumed.get("cursor_out"):
                return items, page_receipts
            token = resumed.get("cursor_out")
            continue
        page: IntradayPage | None = None
        last_error: Exception | None = None
        attempts = 0
        for _attempt in range(MAX_RETRIES):
            attempts += 1
            request_started_at = _now()
            if durable is not None:
                durable.attempt_started(
                    page_key,
                    attempts,
                    {
                        "provider": provider.provider_name,
                        "feed": provider.feed,
                        "endpoint": expected_endpoint,
                        "symbols_sha256": _sha256(list(symbols)),
                        "request_start": start,
                        "request_end": end,
                        "cursor_in": token,
                    },
                )
            try:
                page = getattr(provider, method)(
                    symbols, start, end, call_config, page_token=token
                )
                response_received_at = _now()
                break
            except KeyboardInterrupt as exc:
                if durable is not None:
                    durable.attempt_failed(page_key, attempts, str(exc) or "interrupted in flight")
                raise
            except Exception as exc:  # provider errors become a receipt failure
                last_error = exc
                if durable is not None:
                    durable.attempt_failed(page_key, attempts, str(exc))
        if page is None:
            raise Ops05Error(f"{method} failed after {MAX_RETRIES} retries: {last_error}")
        if (
            page.provider != provider.provider_name
            or page.feed != provider.feed
            or page.endpoint != expected_endpoint
        ):
            raise Ops05Error("provider page identity does not match the declared request")
        page_items = [dict(item) for item in page.items]
        computed_page_hash = _sha256(page_items)
        page_bytes = sum(len(_canonical_json(item)) for item in page_items)
        byte_counter[0] += page_bytes
        if byte_counter[0] > MAX_BYTES:
            raise Ops05Error("raw provider payload exceeds the 64 MiB bound")
        event_counter[0] += len(page.items)
        if event_counter[0] > MAX_EVENTS:
            raise Ops05Error("derived event cap exceeded")
        page_receipts.append(
            {
                "page_number": page_number,
                "endpoint": page.endpoint,
                "provider": page.provider,
                "feed": page.feed,
                "item_count": len(page.items),
                "raw_payload_hash_sha256": computed_page_hash,
                "provider_raw_payload_hash_sha256": page.raw_payload_hash_sha256,
                "request_id": page.request_id,
                "cursor_in": token,
                "cursor_out": page.next_page_token,
                "attempts": attempts,
                "observed_item_bytes": page_bytes,
                "request_start": start,
                "request_end": end,
                "request_started_at": request_started_at,
                "response_received_at": response_received_at,
                "ingested_at": _now(),
                "raw_payload_items": page_items,
            }
        )
        if durable is not None:
            durable.note_runtime(started_at)
            durable.commit_page(page_key, page_receipts[-1], page_bytes=page_bytes)
        page_counter[0] += 1
        items.extend(
            {
                **item,
                "__ops05_page_number": page_number,
                "__ops05_page_row_index": row_index,
                "__ops05_page_hash_sha256": computed_page_hash,
            }
            for row_index, item in enumerate(page_items)
        )
        if len(page_receipts) > MAX_PAGES:
            raise Ops05Error("provider page cap exceeded")
        if not page.next_page_token:
            return items, page_receipts
        token = page.next_page_token
    raise Ops05Error("provider page cap exceeded")


def _process_tree_rss_bytes() -> int | None:
    if os.name != "nt":
        return None

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("thDefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.wintypes.LONG),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.wintypes.WCHAR * 260),
        ]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.wintypes.HANDLE(-1).value:
        return None
    try:
        first = ProcessEntry()
        first.dwSize = ctypes.sizeof(ProcessEntry)
        parents: dict[int, int] = {}
        if not kernel.Process32FirstW(snapshot, ctypes.byref(first)):
            return None
        while True:
            parents[int(first.th32ProcessID)] = int(first.th32ParentProcessID)
            if not kernel.Process32NextW(snapshot, ctypes.byref(first)):
                break
    finally:
        kernel.CloseHandle(snapshot)
    own_pid = os.getpid()
    pids = {own_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in pids and pid not in pids:
                pids.add(pid)
                changed = True
    total = 0
    for pid in sorted(pids):
        handle = kernel.OpenProcess(0x0410, False, pid)
        if not handle:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(ProcessMemoryCounters)
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            total += int(counters.WorkingSetSize)
        finally:
            kernel.CloseHandle(handle)
    return total


def _check_runtime(started_at: float) -> None:
    if wall_clock.monotonic() - started_at > MAX_WALL_SECONDS:
        raise Ops05Error("OPS05 wall-time cap exceeded")
    rss = _process_tree_rss_bytes()
    if rss is None:
        raise Ops05Error("process-tree RSS measurement unavailable")
    if rss > MAX_RSS_BYTES:
        raise Ops05Error("OPS05 process-tree RSS cap exceeded")


def _produce_historical_bars(
    *,
    market_date: str,
    census: Sequence[Mapping[str, Any]],
    provider: HistoricalBarsProvider,
    config: Any,
    output_root: str | Path,
    source_config_hash: str,
    capture_receipt_hash: str,
    durable: _DurableCapture | None = None,
) -> dict[str, Any]:
    """Produce a bounded delayed archive using a supplied provider adapter."""

    if provider.provider_name != "alpaca" or provider.feed != "sip":
        raise Ops05Error("OPS05 requires the existing Alpaca SIP provider; no feed fallback")
    if len(source_config_hash) != 64 or len(capture_receipt_hash) != 64:
        raise Ops05Error("source and capture identities must be SHA-256 values")
    windows = build_windows(market_date)
    census_rows, sampling = select_movers(census)
    mover_symbols = sampling["sampled_symbols"]
    if set(mover_symbols) & set(PANEL_SYMBOLS):
        raise Ops05Error("original mover census overlaps the fixed reference panel")
    symbols = tuple(PANEL_SYMBOLS) + tuple(mover_symbols)
    if len(symbols) > len(PANEL_SYMBOLS) + MAX_MOVER_SYMBOLS:
        raise Ops05Error("bounded symbol count exceeded")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if durable is None:
        identity = {
            "market_date": market_date,
            "provider": provider.provider_name,
            "feed": provider.feed,
            "source_config_sha256": source_config_hash,
            "capture_receipt_sha256": capture_receipt_hash,
            "census_sha256": _sha256(census_rows),
            "windows": {key: value.as_dict() for key, value in windows.items()},
        }
        durable = _DurableCapture(root=root, identity=identity)
    started_at = wall_clock.monotonic()
    page_counter = [0]
    event_counter = [0]
    bars: list[dict[str, Any]] = []
    boundary_events: list[dict[str, Any]] = []
    page_receipts: list[dict[str, Any]] = []
    byte_counter = [0]
    seen: set[tuple[str, str]] = set()
    for window_name in ("full_session", "prior_close"):
        window = windows[window_name]
        start = window.start.isoformat().replace("+00:00", "Z")
        end = window.end.isoformat().replace("+00:00", "Z")
        try:
            items, pages = _fetch_pages(
                provider,
                "get_bars_page",
                symbols,
                start,
                end,
                config,
                page_counter=page_counter,
                event_counter=event_counter,
                byte_counter=byte_counter,
                started_at=started_at,
                expected_endpoint="bars",
                logical_key_prefix=window_name,
                durable=durable,
            )
        except BaseException as exc:
            durable.fail(window_name, exc)
            raise
        page_receipts.extend([{**page, "window": window_name} for page in pages])
        for item in items:
            symbol = str(item.get("symbol") or item.get("S") or "").upper()
            if symbol not in symbols:
                raise Ops05Error("provider returned a symbol outside the declared cohort")
            timestamp = _parse_timestamp(_bar_value(item, "timestamp", "t"), label="bar timestamp")
            if timestamp == window.end:
                boundary_events.append(
                    {
                        "window": window_name,
                        "classification": "provider_inclusive_boundary_excluded_from_half_open",
                        "symbol": symbol,
                        "item": dict(item),
                    }
                )
                if len(bars) + len(boundary_events) > MAX_EVENTS:
                    raise Ops05Error("derived event cap exceeded")
                continue
            if timestamp < window.start or timestamp > window.end:
                raise Ops05Error("provider returned an out-of-window event")
            event = _normalize_bar(
                item,
                window=window,
                source_hash=capture_receipt_hash,
                source_identity=f"{provider.provider_name}:{provider.feed}",
            )
            key = (event["symbol"], event["event_time"])
            if key in seen:
                continue
            seen.add(key)
            bars.append(event)
            if len(bars) + len(boundary_events) > MAX_EVENTS:
                raise Ops05Error("derived event cap exceeded")
        durable.checkpoint_derived(bars, boundary_events)
    ca_start = windows["corporate_actions"].start.isoformat().replace("+00:00", "Z")
    ca_end = windows["corporate_actions"].end.isoformat().replace("+00:00", "Z")
    try:
        ca_items, ca_pages = _fetch_pages(
            provider,
            "get_corporate_actions_page",
            symbols,
            ca_start,
            ca_end,
            config,
            page_counter=page_counter,
            event_counter=event_counter,
            byte_counter=byte_counter,
            started_at=started_at,
            expected_endpoint="corporate_actions",
            logical_key_prefix="corporate_actions",
            durable=durable,
        )
    except BaseException as exc:
        durable.fail("corporate_actions", exc)
        raise
    page_receipts.extend([{**page, "window": "corporate_actions"} for page in ca_pages])
    if len(bars) + len(boundary_events) + len(ca_items) > MAX_EVENTS:
        raise Ops05Error("derived event cap exceeded")
    _check_runtime(started_at)
    for row in census_rows:
        row["observed_symbol"] = row["symbol"] in {event["symbol"] for event in bars}
        if not row["observed_symbol"] and row["sampled_for_bars"]:
            row["missingness"] = "no_bar_observed_in_bounded_windows"
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "producer_version": "ops05-bars-2",
        "ingested_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "market_date": market_date,
        "windows": {key: value.as_dict() for key, value in windows.items()},
        "symbols": {
            "reference_panel": list(PANEL_SYMBOLS),
            "sampled_original_movers": mover_symbols,
        },
        "full_census": census_rows,
        "sampling": sampling,
        "source_lineage": {
            "provider": provider.provider_name,
            "feed": provider.feed,
            "source_config_sha256": source_config_hash,
            "capture_receipt_sha256": capture_receipt_hash,
            "provider_window_contract": "alpaca.stock.historical.v1",
            "provider_boundary": "inclusive_request_end; derived_windows_half_open",
        },
        "coverage": {
            "bar_count": len(bars),
            "corporate_action_count": len(ca_items),
            "page_count": len(page_receipts),
            "raw_payload_bytes_observed": byte_counter[0],
            "process_tree_rss_bytes_observed": _process_tree_rss_bytes(),
            "wall_seconds_observed": round(wall_clock.monotonic() - started_at, 6),
            "boundary_event_count": len(boundary_events),
            "timing_class": "delayed_historical",
            "decision_eligible": False,
            "development_only": True,
            "fresh_holdout": False,
            "cross_close_horizon_minutes": 600,
            "cross_close_censored": True,
            "bars_do_not_prove_intrabar_quote_or_execution_path": True,
        },
        "limits": {
            "max_pages_total": MAX_PAGES,
            "max_retries_per_page": MAX_RETRIES,
            "max_events": MAX_EVENTS,
            "max_bytes": MAX_BYTES,
            "max_rss_bytes_process_tree": MAX_RSS_BYTES,
            "max_wall_seconds": MAX_WALL_SECONDS,
            "max_mover_symbols": MAX_MOVER_SYMBOLS,
        },
        "pages": page_receipts,
        "corporate_actions": {
            "status": "EMPTY" if not ca_items else "CAPTURED",
            "coverage_state": "HEALTHY_EMPTY_RESPONSE" if not ca_items else "CAPTURED",
            "items": ca_items,
        },
        "consumer_handoff": {
            "status": "ADAPTER_REQUIRED",
            "consumer": "intraday_scanner.alpha.v6.observation_dataset.build_observation_dataset",
            "raw_events_path": "raw-bars.jsonl",
            "producer_receipt_path": "receipt.json",
            "raw_event_shape": "ops05_bar_event_v1",
            "alpha_v6_required_fields_missing": ["scope", "kind"],
            "decision_identity_required": True,
            "feature_availability_before_decision_required": True,
            "label_availability_after_decision_required": True,
            "reason": (
                "OPS05 delayed-bars receipt is not producer_receipt.v1 and does not "
                "assert a complete UniverseManifest or READY collection"
            ),
            "eligible_labels": 0,
        },
        "raw_event_stream_sha256": _sha256(bars),
        "status": "CAPTURED" if bars else "EMPTY",
        "research_only": True,
        "broker_execution": "disabled",
    }
    raw_bars_text = "\n".join(json.dumps(row, sort_keys=True) for row in bars) + (
        "\n" if bars else ""
    )
    boundary_text = "\n".join(
        json.dumps(row, sort_keys=True) for row in boundary_events
    ) + ("\n" if boundary_events else "")
    census_text = json.dumps(census_rows, sort_keys=True, indent=2) + "\n"
    receipt["coverage"]["persisted_output_bytes"] = 0
    receipt_text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    persisted_bytes = sum(
        len(value.encode("utf-8"))
        for value in (raw_bars_text, boundary_text, census_text, receipt_text)
    )
    receipt["coverage"]["persisted_output_bytes"] = persisted_bytes
    receipt_text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    persisted_bytes = sum(
        len(value.encode("utf-8"))
        for value in (raw_bars_text, boundary_text, census_text, receipt_text)
    )
    receipt["coverage"]["persisted_output_bytes"] = persisted_bytes
    if persisted_bytes > MAX_BYTES:
        raise Ops05Error("persisted output exceeds the 64 MiB bound")
    binding = {
        "schema_version": "dawnstrike.ops05.capture_binding.v1",
        "market_date": market_date,
        "provider": provider.provider_name,
        "feed": provider.feed,
        "source_config_sha256": source_config_hash,
        "capture_receipt_sha256": capture_receipt_hash,
        "raw_event_stream_sha256": receipt["raw_event_stream_sha256"],
        "pages": [
            {
                key: page[key]
                for key in (
                    "window",
                    "page_number",
                    "endpoint",
                    "provider",
                    "feed",
                    "item_count",
                    "raw_payload_hash_sha256",
                    "request_start",
                    "request_end",
                )
            }
            for page in page_receipts
        ],
    }
    binding["binding_sha256"] = _sha256(binding)
    binding_text = json.dumps(binding, sort_keys=True, indent=2) + "\n"
    binding_bytes = len(binding_text.encode("utf-8"))
    durable_bytes = durable.ledger_bytes()
    receipt["coverage"]["durable_journal_bytes"] = durable_bytes
    for _ in range(3):
        receipt_text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        persisted_bytes = sum(
            len(value.encode("utf-8"))
            for value in (raw_bars_text, boundary_text, census_text, receipt_text)
        ) + binding_bytes
        receipt["coverage"]["persisted_output_bytes"] = persisted_bytes
    receipt_text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    if persisted_bytes + durable_bytes > MAX_BYTES:
        raise Ops05Error("persisted capture binding exceeds the 64 MiB bound")
    for path, text in (
        (root / "raw-bars.jsonl", raw_bars_text),
        (root / "boundary-events.jsonl", boundary_text),
        (root / "universe-census.json", census_text),
        (root / "receipt.json", receipt_text),
        (root / "capture-binding.json", binding_text),
    ):
        if path.is_file() and path.read_text(encoding="utf-8") != text:
            raise Ops05Error(f"immutable OPS05 artifact conflict: {path.name}")
        if not path.is_file():
            _atomic_write(path, text.encode("utf-8"))
    durable.complete()
    return receipt


def produce_historical_bars(
    *,
    market_date: str,
    census: Sequence[Mapping[str, Any]],
    provider: HistoricalBarsProvider,
    config: Any,
    output_root: str | Path,
    source_config_hash: str,
    capture_receipt_hash: str,
    resume_across_roots: bool = False,
) -> dict[str, Any]:
    """Run or resume one durable, bounded OPS05 capture."""
    if provider.provider_name != "alpaca" or provider.feed != "sip":
        raise Ops05Error("OPS05 requires the existing Alpaca SIP provider; no feed fallback")
    if len(source_config_hash) != 64 or len(capture_receipt_hash) != 64:
        raise Ops05Error("source and capture identities must be SHA-256 values")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    windows = build_windows(market_date)
    census_rows, _sampling = select_movers(census)
    identity = {
        "market_date": market_date,
        "provider": provider.provider_name,
        "feed": provider.feed,
        "source_config_sha256": source_config_hash,
        "capture_receipt_sha256": capture_receipt_hash,
        "census_sha256": _sha256(census_rows),
        "windows": {key: value.as_dict() for key, value in windows.items()},
    }
    durable = _DurableCapture(
        root=root, identity=identity, resume_across_roots=resume_across_roots
    )
    existing = durable.existing_receipt()
    if existing is not None:
        return existing
    try:
        with durable:
            return _produce_historical_bars(
                market_date=market_date,
                census=census,
                provider=provider,
                config=config,
                output_root=root,
                source_config_hash=source_config_hash,
                capture_receipt_hash=capture_receipt_hash,
                durable=durable,
            )
    except BaseException as exc:
        if durable._held:
            durable.fail("capture", exc)
        raise
