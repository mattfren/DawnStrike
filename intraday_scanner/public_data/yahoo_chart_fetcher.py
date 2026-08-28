"""Optional read-only Yahoo Finance chart fetch/cache adapter."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request

from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.v2.data.market import MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data.yahoo_chart import (
    DEFAULT_YAHOO_CHART_SYMBOLS,
    YahooChartFetchResult,
    _bars_from_payload,
    dataset_from_yahoo_chart_payloads,
)

YAHOO_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


@dataclass(frozen=True)
class _SymbolFetchResult:
    symbol: str
    payload: dict[str, Any] | None
    attempts: int
    error: str | None = None
    from_cache: bool = False
    cache_path: Path | None = None


def fetch_yahoo_chart_daily_dataset(
    *,
    symbols: tuple[str, ...] = DEFAULT_YAHOO_CHART_SYMBOLS,
    cache_dir: Path,
    range_period: str = "2y",
    interval: str = "1d",
    timeout_seconds: float = 20.0,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0.25,
    max_workers: int = 16,
    max_requests_per_second: float | None = None,
    time_budget_seconds: float | None = None,
    required_bar_date: date | None = None,
) -> YahooChartFetchResult:
    """Fetch public daily OHLCV bars and cache raw payloads for audit.

    Acquisition is bounded by ``max_workers`` and, when supplied, a global
    request-rate limit and request-admission budget.  Results are merged in requested
    symbol order, never completion order.  Successful symbols from an
    incomplete run are retained under ``.partial`` for a later idempotent
    resume; canonical cache files are promoted only after every requested
    symbol has a valid payload.  No partial dataset is presented as a complete
    source artifact.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds cannot be negative")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    if max_requests_per_second is not None and max_requests_per_second <= 0:
        raise ValueError("max_requests_per_second must be positive when supplied")
    if time_budget_seconds is not None and time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive when supplied")
    cache_dir.mkdir(parents=True, exist_ok=True)
    requested_symbols = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    warnings: list[str] = [
        "public_yahoo_chart: free public chart data; not an institutional feed",
        "public_yahoo_chart: no independent corporate-action reconciliation",
    ]
    urls: dict[str, str] = {}
    urls.update(
        {
            symbol: _chart_url(symbol, range_period=range_period, interval=interval)
            for symbol in requested_symbols
        }
    )
    deadline = time.monotonic() + time_budget_seconds if time_budget_seconds else None
    request_gate = _RequestRateGate(max_requests_per_second)

    # The executor is deliberately bounded.  Futures are consumed in the
    # original symbol order below, so network completion order cannot change
    # warning order, CSV order, or any downstream hash.
    executor = ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(requested_symbols))))
    futures: dict[str, Future[_SymbolFetchResult]] = {
        symbol: executor.submit(
            _fetch_symbol,
            symbol=symbol,
            url=urls[symbol],
            cache_dir=cache_dir,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            required_bar_date=required_bar_date,
            deadline=deadline,
            request_gate=request_gate,
        )
        for symbol in requested_symbols
    }
    results: dict[str, _SymbolFetchResult] = {}
    try:
        for symbol in requested_symbols:
            future = futures[symbol]
            try:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                result = future.result(timeout=remaining)
            except TimeoutError:
                result = _SymbolFetchResult(
                    symbol=symbol,
                    payload=None,
                    attempts=0,
                    error="acquisition wall-clock budget exhausted",
                )
            except Exception as exc:  # defensive isolation of one symbol
                result = _SymbolFetchResult(
                    symbol=symbol,
                    payload=None,
                    attempts=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results[symbol] = result
    finally:
        for future in futures.values():
            if not future.done():
                future.cancel()
        # In-flight urllib calls are bounded by timeout_seconds.  Always join
        # workers so a declared admission budget never leaks background threads
        # past the caller's terminal result.
        executor.shutdown(wait=True, cancel_futures=True)

    payloads: dict[str, dict[str, Any]] = {}
    for symbol in requested_symbols:
        result = results[symbol]
        if result.payload is None:
            detail = result.error or "unknown acquisition error"
            warnings.append(
                f"{symbol}: public chart fetch failed after "
                f"{result.attempts or max_attempts} attempts "
                f"({detail})"
            )
            continue
        payloads[symbol] = result.payload
        if result.from_cache:
            warnings.append(f"{symbol}: resumed from immutable partial cache")
        elif result.attempts > 1:
            warnings.append(
                f"{symbol}: public chart fetch succeeded after {result.attempts} attempts"
            )

    # Parse once before writing anything.  A response with no usable bars is
    # not an admitted symbol and is never promoted to the canonical cache.
    dataset = dataset_from_yahoo_chart_payloads(
        payloads,
        dataset_id=f"public_yahoo_chart_{range_period}_{interval}",
        source_kind="public_yahoo_chart",
        source_refs=tuple(urls[symbol] for symbol in requested_symbols),
        warnings=tuple(warnings),
    )
    missing_symbols = tuple(
        symbol for symbol in requested_symbols if symbol not in dataset.bars_by_symbol
    )
    if missing_symbols:
        warnings.append(
            "public_yahoo_chart: incomplete requested symbol set; canonical cache not updated; "
            f"missing={list(missing_symbols)}"
        )
        _write_partial_payloads(
            cache_dir,
            {symbol: payloads[symbol] for symbol in requested_symbols if symbol in payloads},
            required_bar_date=required_bar_date,
        )
        dataset = _dataset_with_warnings(dataset, warnings)
        return YahooChartFetchResult(
            dataset=dataset,
            raw_payload_paths=(),
            warnings=dataset.warnings,
        )

    raw_paths: list[Path] = []
    source_refs: list[str] = []
    for normalized in requested_symbols:
        raw_path = _write_immutable_payload(
            cache_dir,
            normalized,
            payloads[normalized],
        )
        raw_paths.append(raw_path)
        source_refs.extend((urls[normalized], raw_path.as_posix()))

    csv_path = cache_dir / "public_yahoo_ohlcv.csv"
    write_ohlcv_csv(dataset, csv_path)
    dataset = MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol=dataset.bars_by_symbol,
        source_path=csv_path.as_posix(),
        warnings=dataset.warnings,
        source_refs=(csv_path.as_posix(), *source_refs),
    )
    return YahooChartFetchResult(
        dataset=dataset,
        raw_payload_paths=tuple(raw_paths),
        warnings=dataset.warnings,
    )


class _RequestRateGate:
    """Small process-local pacing gate shared by all bounded workers."""

    def __init__(self, max_requests_per_second: float | None) -> None:
        self._interval = (
            0.0 if max_requests_per_second is None else 1.0 / max_requests_per_second
        )
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self, deadline: float | None) -> bool:
        if self._interval <= 0:
            return deadline is None or time.monotonic() < deadline
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_at)
            self._next_at = scheduled + self._interval
        if deadline is not None and scheduled >= deadline:
            return False
        delay = scheduled - time.monotonic()
        if delay > 0:
            if deadline is not None and time.monotonic() + delay >= deadline:
                return False
            time.sleep(delay)
        return deadline is None or time.monotonic() < deadline


def _fetch_symbol(
    *,
    symbol: str,
    url: str,
    cache_dir: Path,
    timeout_seconds: float,
    max_attempts: int,
    retry_backoff_seconds: float,
    required_bar_date: date | None,
    deadline: float | None,
    request_gate: _RequestRateGate,
) -> _SymbolFetchResult:
    cached = _read_cached_payload(
        cache_dir,
        symbol,
        required_bar_date=required_bar_date,
    )
    if cached is not None:
        payload, cache_path = cached
        return _SymbolFetchResult(
            symbol=symbol,
            payload=payload,
            attempts=0,
            from_cache=True,
            cache_path=cache_path,
        )
    last_error: Exception | None = None
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        if deadline is not None and time.monotonic() >= deadline:
            return _SymbolFetchResult(
                symbol=symbol,
                payload=None,
                attempts=attempts - 1,
                error="acquisition wall-clock budget exhausted",
            )
        if not request_gate.wait(deadline):
            return _SymbolFetchResult(
                symbol=symbol,
                payload=None,
                attempts=attempts - 1,
                error="acquisition wall-clock budget exhausted",
            )
        try:
            payload = _fetch_json(url, timeout_seconds=timeout_seconds)
            bars, parse_warnings = _bars_from_payload(symbol, payload)
            del parse_warnings
            if not bars:
                raise ValueError("response contained no valid daily bars")
            if required_bar_date is not None and not any(
                bar.timestamp.date() == required_bar_date for bar in bars
            ):
                raise ValueError(
                    f"response lacks required completed bar {required_bar_date.isoformat()}"
                )
            return _SymbolFetchResult(symbol=symbol, payload=payload, attempts=attempts)
        except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempts < max_attempts and retry_backoff_seconds:
                delay = min(retry_backoff_seconds * (2 ** (attempts - 1)), 4.0)
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                if delay:
                    time.sleep(delay)
    return _SymbolFetchResult(
        symbol=symbol,
        payload=None,
        attempts=attempts,
        error=str(last_error or "unknown error"),
    )


def _dataset_with_warnings(dataset: MarketDataset, warnings: list[str]) -> MarketDataset:
    return MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol=dataset.bars_by_symbol,
        warnings=tuple(dict.fromkeys((*dataset.warnings, *warnings))),
        source_refs=dataset.source_refs,
    )


def _cache_candidates(
    cache_dir: Path,
    symbol: str,
    *,
    required_bar_date: date | None,
) -> tuple[Path, ...]:
    candidates = [
        cache_dir / f"{symbol.lower()}_chart.json",
        *sorted(cache_dir.glob(f"{symbol.lower()}_chart_*.json")),
    ]
    if required_bar_date is not None:
        partial_dir = cache_dir / ".partial" / required_bar_date.isoformat()
        candidates.extend(
            [
                partial_dir / f"{symbol.lower()}_chart.json",
                *sorted(partial_dir.glob(f"{symbol.lower()}_chart_*.json")),
            ]
        )
    return tuple(path for path in candidates if path.is_file())


def _read_cached_payload(
    cache_dir: Path,
    symbol: str,
    *,
    required_bar_date: date | None,
) -> tuple[dict[str, Any], Path] | None:
    # Root cache files predate this bounded-resume implementation.  They are
    # usable only when the caller supplies an exact required date; otherwise a
    # root cache could silently turn a fresh request into stale truth. Partial
    # files are namespaced by that exact date and are never reused undated.
    candidates: list[tuple[Path, dict[str, Any], list[Any]]] = []
    for path in _cache_candidates(
        cache_dir,
        symbol,
        required_bar_date=required_bar_date,
    ):
        if required_bar_date is None:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            bars, _warnings = _bars_from_payload(symbol, payload)
            if not bars:
                continue
            if required_bar_date is not None and not any(
                bar.timestamp.date() == required_bar_date for bar in bars
            ):
                continue
            candidates.append((path, payload, bars))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not candidates:
        return None
    # Choose the newest exact-date bar, with a stable filename tie-breaker.
    # Filesystem mtime is intentionally excluded from the identity.
    path, payload, _bars = max(
        candidates,
        key=lambda item: (max(bar.timestamp for bar in item[2]), item[0].name),
    )
    return payload, path


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_immutable_payload(cache_dir: Path, symbol: str, payload: dict[str, Any]) -> Path:
    content = _canonical_payload_bytes(payload)
    legacy_path = cache_dir / f"{symbol.lower()}_chart.json"
    if legacy_path.exists():
        try:
            if legacy_path.read_bytes() == content:
                return legacy_path
        except OSError:
            pass
        digest = hashlib.sha256(content).hexdigest()[:16]
        target = cache_dir / f"{symbol.lower()}_chart_{digest}.json"
    else:
        target = legacy_path
    _write_exclusive(target, content)
    return target


def _write_partial_payloads(
    cache_dir: Path,
    payloads: dict[str, dict[str, Any]],
    *,
    required_bar_date: date | None,
) -> None:
    if required_bar_date is None:
        return
    partial_dir = cache_dir / ".partial" / required_bar_date.isoformat()
    partial_dir.mkdir(parents=True, exist_ok=True)
    for symbol in sorted(payloads):
        _write_immutable_payload(partial_dir, symbol, payloads[symbol])


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        # Existing bytes are immutable.  A differing payload gets a
        # content-addressed sibling in _write_immutable_payload instead.
        if path.read_bytes() != content:
            raise ValueError(f"immutable Yahoo cache conflict: {path}") from None


def _chart_url(symbol: str, *, range_period: str, interval: str) -> str:
    query = urlencode(
        {
            "range": range_period,
            "interval": interval,
            "includePrePost": "false",
            "events": "history",
        }
    )
    return f"{YAHOO_CHART_BASE_URL.format(symbol=symbol)}?{query}"


def _fetch_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dawnstrike-v2-AlphaLab/1.0 research-only",
        },
    )
    with open_allowlisted_url(
        request,
        timeout=timeout_seconds,
        allowed_hosts=("query1.finance.yahoo.com",),
    ) as response:
        payload = response.read().decode("utf-8")
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise TypeError("expected JSON object")
    return loaded
