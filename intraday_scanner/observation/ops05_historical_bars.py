"""Bounded delayed historical SIP bar production for OPS05.

This module is an observation-only adapter.  It can consume the existing
read-only Alpaca client, but never performs a network request unless the CLI is
explicitly invoked with ``--execute``.  The default and all tests use a fake
page provider so the source contract can be independently checked first.
"""

from __future__ import annotations

import hashlib
import json
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
    present = [key for key in ("selected", "rejected", "unselected") if rows_by_membership.get(key)]
    target = min(limit, sum(len(rows_by_membership[key]) for key in present))
    quotas = {key: 0 for key in rows_by_membership}
    if target >= len(present):
        for key in present:
            quotas[key] = 1
        target -= len(present)
    if target <= 0:
        return quotas
    weights = {key: len(rows_by_membership[key]) for key in present}
    total = sum(weights.values())
    fractions: list[tuple[float, str]] = []
    for key in present:
        raw = target * weights[key] / total
        whole = int(raw)
        quotas[key] += whole
        fractions.append((raw - whole, key))
    remaining = target - sum(quotas.values())
    for _fraction, key in sorted(fractions, key=lambda item: (-item[0], item[1]))[:remaining]:
        quotas[key] += 1
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
    by_membership: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("selected", "rejected", "unselected")
    }
    for raw in census:
        row = dict(raw)
        symbol = str(row.get("symbol") or "").strip().upper()
        membership = str(row.get("membership") or "").strip().lower()
        if not symbol or symbol in seen:
            raise Ops05Error("original census must contain unique non-empty symbols")
        if membership not in by_membership:
            raise Ops05Error("original census membership must be selected/rejected/unselected")
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
    for membership in ("selected", "rejected", "unselected"):
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


def _normalize_bar(item: Mapping[str, Any], *, window: Window, source_hash: str) -> dict[str, Any]:
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
        "source": "alpaca:sip",
        "source_artifact_hash_sha256": source_hash,
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
    started_at: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    page_receipts: list[dict[str, Any]] = []
    token: str | None = None
    for page_number in range(MAX_PAGES):
        _check_runtime(started_at)
        if page_counter[0] >= MAX_PAGES:
            raise Ops05Error("provider page cap exceeded")
        page: IntradayPage | None = None
        last_error: Exception | None = None
        for _attempt in range(MAX_RETRIES):
            try:
                page = getattr(provider, method)(symbols, start, end, config, page_token=token)
                break
            except Exception as exc:  # provider errors become a receipt failure
                last_error = exc
        if page is None:
            raise Ops05Error(f"{method} failed after {MAX_RETRIES} retries: {last_error}")
        page_receipts.append(
            {
                "page_number": page_number,
                "endpoint": page.endpoint,
                "provider": page.provider,
                "feed": page.feed,
                "item_count": len(page.items),
                "raw_payload_hash_sha256": page.raw_payload_hash_sha256,
                "request_id": page.request_id,
                "cursor_in": token,
                "cursor_out": page.next_page_token,
            }
        )
        page_counter[0] += 1
        items.extend(dict(item) for item in page.items)
        if len(page_receipts) > MAX_PAGES:
            raise Ops05Error("provider page cap exceeded")
        if not page.next_page_token:
            return items, page_receipts
        token = page.next_page_token
    raise Ops05Error("provider page cap exceeded")


def _process_tree_rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore[import-not-found]

        process = psutil.Process()
        return process.memory_info().rss + sum(
            child.memory_info().rss for child in process.children(recursive=True)
        )
    except (ImportError, OSError):
        return None


def _check_runtime(started_at: float) -> None:
    if wall_clock.monotonic() - started_at > MAX_WALL_SECONDS:
        raise Ops05Error("OPS05 wall-time cap exceeded")
    rss = _process_tree_rss_bytes()
    if rss is not None and rss > MAX_RSS_BYTES:
        raise Ops05Error("OPS05 process-tree RSS cap exceeded")


def produce_historical_bars(
    *,
    market_date: str,
    census: Sequence[Mapping[str, Any]],
    provider: HistoricalBarsProvider,
    config: Any,
    output_root: str | Path,
    source_config_hash: str,
    capture_receipt_hash: str,
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
    started_at = wall_clock.monotonic()
    page_counter = [0]
    bars: list[dict[str, Any]] = []
    boundary_events: list[dict[str, Any]] = []
    page_receipts: list[dict[str, Any]] = []
    raw_bytes = 0
    seen: set[tuple[str, str]] = set()
    for window_name in ("full_session", "prior_close"):
        window = windows[window_name]
        start = window.start.isoformat().replace("+00:00", "Z")
        end = window.end.isoformat().replace("+00:00", "Z")
        items, pages = _fetch_pages(
            provider,
            "get_bars_page",
            symbols,
            start,
            end,
            config,
            page_counter=page_counter,
            started_at=started_at,
        )
        page_receipts.extend([{**page, "window": window_name} for page in pages])
        raw_bytes += sum(len(_canonical_json(item)) for item in items)
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
                continue
            if timestamp < window.start or timestamp > window.end:
                raise Ops05Error("provider returned an out-of-window event")
            event = _normalize_bar(item, window=window, source_hash=capture_receipt_hash)
            key = (event["symbol"], event["event_time"])
            if key in seen:
                continue
            seen.add(key)
            bars.append(event)
            if len(bars) > MAX_EVENTS:
                raise Ops05Error("derived event cap exceeded")
    ca_start = windows["corporate_actions"].start.isoformat().replace("+00:00", "Z")
    ca_end = windows["corporate_actions"].end.isoformat().replace("+00:00", "Z")
    ca_items, ca_pages = _fetch_pages(
        provider,
        "get_corporate_actions_page",
        symbols,
        ca_start,
        ca_end,
        config,
        page_counter=page_counter,
        started_at=started_at,
    )
    page_receipts.extend([{**page, "window": "corporate_actions"} for page in ca_pages])
    raw_bytes += sum(len(_canonical_json(item)) for item in ca_items)
    if raw_bytes > MAX_BYTES:
        raise Ops05Error("raw provider payload exceeds the 64 MiB bound")
    if len(bars) + len(ca_items) > MAX_EVENTS:
        raise Ops05Error("derived event cap exceeded")
    _check_runtime(started_at)
    for row in census_rows:
        row["observed_symbol"] = row["symbol"] in {event["symbol"] for event in bars}
        if not row["observed_symbol"] and row["sampled_for_bars"]:
            row["missingness"] = "no_bar_observed_in_bounded_windows"
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "producer_version": "ops05-bars-1",
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
            "raw_payload_bytes_observed": raw_bytes,
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
        "raw_event_stream_sha256": _sha256(bars),
        "status": "CAPTURED" if bars else "EMPTY",
        "research_only": True,
        "broker_execution": "disabled",
    }
    (root / "raw-bars.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in bars) + ("\n" if bars else ""),
        encoding="utf-8",
    )
    (root / "boundary-events.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in boundary_events)
        + ("\n" if boundary_events else ""),
        encoding="utf-8",
    )
    (root / "universe-census.json").write_text(
        json.dumps(census_rows, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (root / "receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return receipt
