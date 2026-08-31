"""Optional read-only Yahoo Finance chart fetch/cache adapter."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request

from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.v2.data.market import (
    MAX_MARKET_CSV_BYTES,
    MarketDataset,
    write_ohlcv_csv,
)
from intraday_scanner.v2.data.yahoo_chart import (
    DEFAULT_YAHOO_CHART_SYMBOLS,
    YahooChartFetchResult,
    _bars_from_payload,
    dataset_from_yahoo_chart_payloads,
)

YAHOO_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_CANONICAL_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*$")
_MAX_SYMBOL_LENGTH = 16
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAX_CSV_BYTES = MAX_MARKET_CSV_BYTES
_READ_CHUNK_BYTES = 64 * 1024
# Persisted reservations are expected to cover only the bounded in-flight
# worker horizon. Anything farther ahead is treated as poisoned state so a
# stale lock file cannot consume the entire EOD admission window.
_MAX_PERSISTED_RATE_AHEAD_SECONDS = 5 * 60
# Cross-process callers can be descheduled briefly after admission while the
# host is under load. Reserve a conservative cushion so their actual request
# starts still respect the configured provider rate ceiling.
_RATE_RESERVATION_SAFETY_SECONDS = 0.5
_CACHE_WRITE_LOCK = threading.RLock()
_PROCESS_RATE_LOCK = threading.Lock()
_PROCESS_NEXT_REQUEST_AT = 0.0
_REQUEST_CONTRACT_SCHEMA = "v2.public_yahoo_chart_request.v1"


@dataclass(frozen=True)
class _SymbolFetchResult:
    symbol: str
    payload: dict[str, Any] | None
    attempts: int
    error: str | None = None
    from_cache: bool = False
    cache_path: Path | None = None
    completed_at: float | None = None


def _lock_windows_file(handle: Any, mode_name: str) -> None:
    """Apply a Windows byte-range lock without relying on incomplete stubs."""

    msvcrt_module = importlib.import_module("msvcrt")
    locking = getattr(msvcrt_module, "locking", None)
    mode = getattr(msvcrt_module, mode_name, None)
    if not callable(locking) or not isinstance(mode, int):
        raise OSError("Windows file locking is unavailable")
    locking(handle.fileno(), mode, 1)


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
    minimum_history_bars: int = 0,
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
    if minimum_history_bars < 0:
        raise ValueError("minimum_history_bars cannot be negative")
    requested_symbols = canonicalize_yahoo_symbols(symbols)
    provider_symbols = _provider_symbol_map(requested_symbols)
    cache_root = cache_dir.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = [
        "public_yahoo_chart: free public chart data; not an institutional feed",
        "public_yahoo_chart: no independent corporate-action reconciliation",
    ]
    urls: dict[str, str] = {}
    urls.update(
        {
            symbol: _chart_url(
                provider_symbols[symbol],
                range_period=range_period,
                interval=interval,
            )
            for symbol in requested_symbols
        }
    )
    deadline = time.monotonic() + time_budget_seconds if time_budget_seconds else None
    request_contract = _request_contract(range_period=range_period, interval=interval)
    request_gate = _RequestRateGate(
        max_requests_per_second,
        cache_root=cache_root,
    )

    # The executor is deliberately bounded.  Futures are consumed in the
    # original symbol order below, so network completion order cannot change
    # warning order, CSV order, or any downstream hash.
    worker_count = min(max_workers, max(1, len(requested_symbols)))
    executor: ThreadPoolExecutor | None = None
    if deadline is None:
        executor = ThreadPoolExecutor(max_workers=worker_count)
        futures: dict[str, Future[_SymbolFetchResult]] = {
            symbol: executor.submit(
                _fetch_symbol,
                symbol=symbol,
                provider_symbol=provider_symbols[symbol],
                url=urls[symbol],
                cache_dir=cache_root,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                required_bar_date=required_bar_date,
                minimum_history_bars=minimum_history_bars,
                deadline=deadline,
                request_gate=request_gate,
                cache_root=cache_root,
                request_contract=request_contract,
            )
            for symbol in requested_symbols
        }
    else:
        # A ThreadPoolExecutor owns non-daemon threads and waits for a
        # non-cooperative test/transport adapter during shutdown.  The
        # deadline path uses bounded daemon workers and cancels queued work;
        # workers never write artifacts, so a late transport return cannot
        # mutate the cache after the caller has received its terminal result.
        futures = _start_daemon_fetches(
            requested_symbols=requested_symbols,
            provider_symbols=provider_symbols,
            urls=urls,
            cache_root=cache_root,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            required_bar_date=required_bar_date,
            minimum_history_bars=minimum_history_bars,
            deadline=deadline,
            request_gate=request_gate,
            request_contract=request_contract,
            max_workers=worker_count,
        )
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
        if executor is not None:
            # No deadline means there is no terminal wall-clock promise, so
            # retain the ordinary executor's strong join semantics.
            executor.shutdown(wait=True, cancel_futures=True)

    payloads: dict[str, dict[str, Any]] = {}
    for symbol in requested_symbols:
        result = results[symbol]
        if (
            result.payload is None
            or (
                deadline is not None
                and result.completed_at is not None
                and result.completed_at >= deadline
            )
        ):
            detail = result.error or "unknown acquisition error"
            warnings.append(
                f"{symbol}: public chart fetch failed after "
                f"{result.attempts or max_attempts} attempts "
                f"({detail})"
            )
            continue
        payloads[symbol] = result.payload
        if (
            result.from_cache
            and result.cache_path is not None
            and ".partial" in result.cache_path.parts
        ):
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
        source_refs=tuple(
            reference
            for symbol in requested_symbols
            for reference in (
                f"canonical_symbol:{symbol}",
                f"yahoo_symbol:{provider_symbols[symbol]}",
                urls[symbol],
            )
        ),
        warnings=tuple(warnings),
    )
    if deadline is not None and time.monotonic() >= deadline:
        warnings.append("public_yahoo_chart: acquisition deadline exhausted before artifact writes")
        dataset = _dataset_with_warnings(dataset, warnings)
        return YahooChartFetchResult(
            dataset=dataset,
            raw_payload_paths=(),
            warnings=dataset.warnings,
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
            deadline=deadline,
            request_contract=request_contract,
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
            deadline=deadline,
            request_contract=request_contract,
        )
        raw_paths.append(raw_path)
        source_refs.extend(
            (
                f"canonical_symbol:{normalized}",
                f"yahoo_symbol:{provider_symbols[normalized]}",
                urls[normalized],
                raw_path.as_posix(),
            )
        )

    csv_path = _write_csv_immutable(
        dataset,
        cache_root=cache_root,
        deadline=deadline,
        request_contract=request_contract,
    )
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


def _start_daemon_fetches(
    *,
    requested_symbols: tuple[str, ...],
    provider_symbols: dict[str, str],
    urls: dict[str, str],
    cache_root: Path,
    timeout_seconds: float,
    max_attempts: int,
    retry_backoff_seconds: float,
    required_bar_date: date | None,
    minimum_history_bars: int,
    deadline: float,
    request_gate: _RequestRateGate,
    request_contract: dict[str, object],
    max_workers: int,
) -> dict[str, Future[_SymbolFetchResult]]:
    jobs: queue.Queue[str] = queue.Queue()
    futures: dict[str, Future[_SymbolFetchResult]] = {}
    for symbol in requested_symbols:
        jobs.put(symbol)
        futures[symbol] = Future()

    def worker() -> None:
        while True:
            try:
                symbol = jobs.get_nowait()
            except queue.Empty:
                return
            future = futures[symbol]
            if future.cancelled():
                jobs.task_done()
                continue
            if time.monotonic() >= deadline:
                future.set_result(
                    _SymbolFetchResult(
                        symbol=symbol,
                        payload=None,
                        attempts=0,
                        error="acquisition wall-clock budget exhausted",
                    )
                )
                jobs.task_done()
                continue
            try:
                result = _fetch_symbol(
                    symbol=symbol,
                    provider_symbol=provider_symbols[symbol],
                    url=urls[symbol],
                    cache_dir=cache_root,
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                    required_bar_date=required_bar_date,
                    minimum_history_bars=minimum_history_bars,
                    deadline=deadline,
                    request_gate=request_gate,
                    cache_root=cache_root,
                    request_contract=request_contract,
                )
            except BaseException as exc:  # defensive isolation of one symbol
                if not future.cancelled():
                    future.set_exception(exc)
            else:
                if not future.cancelled():
                    future.set_result(result)
            finally:
                jobs.task_done()

    for _index in range(max_workers):
        threading.Thread(target=worker, daemon=True, name="yahoo-chart-fetch").start()
    return futures


class _RequestRateGate:
    """Process-wide pacing gate shared by all concurrent fetch invocations."""

    def __init__(
        self,
        max_requests_per_second: float | None,
        *,
        cache_root: Path | None = None,
    ) -> None:
        self._interval = (
            0.0 if max_requests_per_second is None else 1.0 / max_requests_per_second
        )
        self._cache_root = cache_root

    def wait(self, deadline: float | None) -> bool:
        if self._interval <= 0:
            return deadline is None or time.monotonic() < deadline
        global _PROCESS_NEXT_REQUEST_AT
        with _PROCESS_RATE_LOCK:
            now = time.monotonic()
            scheduled = max(now, _PROCESS_NEXT_REQUEST_AT)
            if self._cache_root is not None:
                with _cross_process_rate_lock(self._cache_root, deadline=deadline) as state:
                    now_wall = time.time()
                    scheduled_wall = max(now_wall, float(state[0]))
                    state[0] = (
                        scheduled_wall
                        + self._interval
                        + _RATE_RESERVATION_SAFETY_SECONDS
                    )
                    scheduled = max(
                        scheduled,
                        now + max(0.0, scheduled_wall - now_wall),
                    )
            _PROCESS_NEXT_REQUEST_AT = max(_PROCESS_NEXT_REQUEST_AT, scheduled + self._interval)
        if deadline is not None and scheduled >= deadline:
            return False
        delay = scheduled - time.monotonic()
        if delay > 0:
            if deadline is not None and time.monotonic() + delay >= deadline:
                return False
            time.sleep(delay)
        return deadline is None or time.monotonic() < deadline


@contextmanager
def _cross_process_rate_lock(cache_root: Path, *, deadline: float | None):
    lock_path = cache_root / ".yahoo_rate_gate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        while handle.seek(0, os.SEEK_END) == 0:
            try:
                handle.write(b"0")
                handle.flush()
            except PermissionError:
                # Another process may be initializing and locking the same
                # one-byte file. Wait for its durable seed rather than
                # failing the request admission path spuriously.
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Yahoo rate-gate initialization exceeded deadline") from None
                time.sleep(0.01)
            else:
                break
        handle.seek(0)
        if os.name == "nt":
            while True:
                try:
                    _lock_windows_file(handle, "LK_NBLCK")
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Yahoo rate-gate lock exceeded acquisition deadline"
                        ) from None
                    time.sleep(0.01)
        else:  # pragma: no cover - exercised on POSIX CI only
            fcntl_module = importlib.import_module("fcntl")
            flock = getattr(fcntl_module, "flock", None)
            lock_ex = getattr(fcntl_module, "LOCK_EX", None)
            lock_nb = getattr(fcntl_module, "LOCK_NB", None)
            if not callable(flock) or not isinstance(lock_ex, int) or not isinstance(lock_nb, int):
                raise OSError("POSIX file locking is unavailable")

            while True:
                try:
                    flock(handle.fileno(), lock_ex | lock_nb)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Yahoo rate-gate lock exceeded acquisition deadline"
                        ) from None
                    time.sleep(0.01)
        try:
            handle.seek(0)
            raw = handle.read(128).decode("ascii", errors="ignore").strip()
            try:
                persisted = float(raw)
            except ValueError:
                persisted = 0.0
            now_wall = time.time()
            if not math.isfinite(persisted) or not (
                now_wall - 3600
                <= persisted
                <= now_wall + _MAX_PERSISTED_RATE_AHEAD_SECONDS
            ):
                persisted = now_wall
            state = [persisted]
            yield state
            handle.seek(0)
            handle.truncate()
            handle.write(f"{state[0]:.9f}".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if os.name == "nt":
                handle.seek(0)
                _lock_windows_file(handle, "LK_UNLCK")
            else:  # pragma: no cover - exercised on POSIX CI only
                fcntl_module = importlib.import_module("fcntl")
                flock = getattr(fcntl_module, "flock", None)
                lock_un = getattr(fcntl_module, "LOCK_UN", None)
                if not callable(flock) or not isinstance(lock_un, int):
                    raise OSError("POSIX file locking is unavailable")
                flock(handle.fileno(), lock_un)


def _fetch_symbol(
    *,
    symbol: str,
    provider_symbol: str,
    url: str,
    cache_dir: Path,
    timeout_seconds: float,
    max_attempts: int,
    retry_backoff_seconds: float,
    required_bar_date: date | None,
    minimum_history_bars: int,
    deadline: float | None,
    request_gate: _RequestRateGate,
    cache_root: Path,
    request_contract: dict[str, object],
) -> _SymbolFetchResult:
    cached = _read_cached_payload(
        cache_root,
        symbol,
        provider_symbol=provider_symbol,
        required_bar_date=required_bar_date,
        minimum_history_bars=minimum_history_bars,
        deadline=deadline,
        cache_root=cache_root,
        expected_request_contract=request_contract,
    )
    if cached is not None:
        payload, cache_path = cached
        completed_at = time.monotonic()
        if deadline is not None and completed_at >= deadline:
            return _SymbolFetchResult(
                symbol=symbol,
                payload=None,
                attempts=0,
                error="acquisition request-admission budget exhausted",
                completed_at=completed_at,
            )
        return _SymbolFetchResult(
            symbol=symbol,
            payload=payload,
            attempts=0,
            from_cache=True,
            cache_path=cache_path,
            completed_at=completed_at,
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
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                return _SymbolFetchResult(
                    symbol=symbol,
                    payload=None,
                    attempts=attempts - 1,
                    error="acquisition wall-clock budget exhausted",
                )
            fetch_kwargs: dict[str, Any] = {
                "timeout_seconds": (
                    timeout_seconds
                    if remaining is None
                    else min(timeout_seconds, remaining)
                )
            }
            # Keep test/in-process adapters that implement the historical
            # private signature usable, while the production transport gets
            # the total deadline for bounded body reads.
            if "deadline" in inspect.signature(_fetch_json).parameters:
                fetch_kwargs["deadline"] = deadline
            payload = _fetch_json(url, **fetch_kwargs)
            completed_at = time.monotonic()
            if deadline is not None and completed_at >= deadline:
                return _SymbolFetchResult(
                    symbol=symbol,
                    payload=None,
                    attempts=attempts,
                    error="response completed after request-admission budget",
                    completed_at=completed_at,
                )
            _validate_payload_symbol(provider_symbol, payload)
            bars, parse_warnings = _bars_from_payload(symbol, payload)
            del parse_warnings
            if not bars:
                raise ValueError("response contained no valid daily bars")
            if not _bars_meet_admission_contract(
                bars,
                required_bar_date=required_bar_date,
                minimum_history_bars=minimum_history_bars,
            ):
                raise ValueError(
                    f"response lacks governed completed-bar/history requirements for "
                    f"{required_bar_date.isoformat() if required_bar_date else 'request'}"
                )
            return _SymbolFetchResult(
                symbol=symbol,
                payload=payload,
                attempts=attempts,
                completed_at=completed_at,
            )
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
    candidates = sorted(cache_dir.glob(f"{symbol.lower()}_chart_*.json"))
    if required_bar_date is not None:
        partial_dir = cache_dir / ".partial" / required_bar_date.isoformat()
        candidates.extend(sorted(partial_dir.glob(f"{symbol.lower()}_chart_*.json")))
    return tuple(
        path
        for path in candidates
        if path.is_file() and _content_addressed_cache_name(path, symbol)
    )


def _read_cached_payload(
    cache_dir: Path,
    symbol: str,
    *,
    provider_symbol: str,
    required_bar_date: date | None,
    minimum_history_bars: int,
    deadline: float | None,
    cache_root: Path,
    expected_request_contract: dict[str, object] | None = None,
) -> tuple[dict[str, Any], Path] | None:
    # Only full-digest content-addressed objects are admissible. Legacy mutable
    # aliases are intentionally ignored, even when their bytes look valid.
    candidates: list[tuple[Path, dict[str, Any], list[Any], str]] = []
    for path in _cache_candidates(
        cache_dir,
        symbol,
        required_bar_date=required_bar_date,
    ):
        _assert_cache_path(cache_root, path)
        if required_bar_date is None:
            continue
        try:
            expected_digest = _cache_digest_from_name(path, symbol)
            _validate_cache_contract(
                path,
                expected_request_contract,
                required=False,
            )
            content = _bounded_file_read(path, deadline=deadline)
            actual_digest = hashlib.sha256(content).hexdigest()
            if actual_digest != expected_digest:
                # Reads are intentionally side-effect free: deadline workers
                # may still be unwinding a transport after the caller returns.
                # The immutable writer quarantines this object before repair.
                continue
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict):
                continue
            _validate_payload_symbol(provider_symbol, payload)
            bars, _warnings = _bars_from_payload(symbol, payload)
            if not _bars_meet_admission_contract(
                bars,
                required_bar_date=required_bar_date,
                minimum_history_bars=minimum_history_bars,
            ):
                continue
            candidates.append((path, payload, bars, expected_digest))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not candidates:
        return None
    # Choose the newest exact-date bar, with a stable filename tie-breaker.
    # Filesystem mtime is intentionally excluded from the identity.
    path, payload, _bars, _digest = max(
        candidates,
        key=lambda item: (
            len({bar.timestamp for bar in item[2]}),
            (
                max(bar.timestamp for bar in item[2])
                - min(bar.timestamp for bar in item[2])
            ).total_seconds(),
            item[3],
        ),
    )
    return payload, path


def _content_addressed_cache_name(path: Path, symbol: str) -> bool:
    prefix = f"{symbol.lower()}_chart_"
    return (
        path.name.startswith(prefix)
        and path.name.endswith(".json")
        and len(path.name) == len(prefix) + 64 + len(".json")
        and all(character in "0123456789abcdef" for character in path.stem[len(prefix) :])
    )


def _cache_digest_from_name(path: Path, symbol: str) -> str:
    if not _content_addressed_cache_name(path, symbol):
        raise ValueError("Yahoo cache object is not a full-digest content-addressed file")
    return path.stem[len(f"{symbol.lower()}_chart_") :]


def _bounded_file_read(
    path: Path,
    *,
    deadline: float | None,
    max_bytes: int = _MAX_PAYLOAD_BYTES,
) -> bytes:
    if path.stat().st_size > max_bytes:
        raise ValueError("Yahoo cache object exceeds maximum payload size")
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Yahoo cache read exceeded acquisition deadline")
            chunk = handle.read(min(_READ_CHUNK_BYTES, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Yahoo cache object exceeds maximum payload size")
            chunks.append(chunk)
    return b"".join(chunks)


def _ensure_before_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("acquisition deadline exhausted before artifact mutation")


def _bars_meet_admission_contract(
    bars: list[Any],
    *,
    required_bar_date: date | None,
    minimum_history_bars: int,
) -> bool:
    unique_timestamps = {bar.timestamp for bar in bars}
    if len(unique_timestamps) < minimum_history_bars or not bars:
        return False
    if required_bar_date is None:
        return True
    dates = [bar.timestamp.date() for bar in bars]
    return max(dates) == required_bar_date and required_bar_date in dates


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_immutable_payload(
    cache_dir: Path,
    symbol: str,
    payload: dict[str, Any],
    *,
    deadline: float | None = None,
    request_contract: dict[str, object] | None = None,
) -> Path:
    _ensure_before_deadline(deadline)
    cache_root = cache_dir.resolve()
    _validate_symbol(symbol)
    content = _canonical_payload_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()
    content_path = cache_dir / f"{symbol.lower()}_chart_{digest}.json"
    # The digest path is the sole authoritative source identity.  The old
    # ``<symbol>_chart.json`` alias is intentionally never returned: another
    # process may replace it between acquisition and DataTruth hashing.
    _assert_cache_path(cache_root, content_path)
    with _CACHE_WRITE_LOCK:
        if content_path.exists():
            try:
                existing = _bounded_file_read(content_path, deadline=None)
            except ValueError:
                _quarantine_corrupt_cache(content_path, cache_root, expected_digest=digest)
                existing = None
            if existing is None:
                existing = b""
            existing_digest = hashlib.sha256(existing).hexdigest()
            if existing != content:
                if existing_digest == digest:
                    raise ValueError(
                        "Yahoo cache digest collision: authoritative object bytes differ"
                    )
                _quarantine_corrupt_cache(
                    content_path, cache_root, expected_digest=digest
                )
        _write_exclusive(
            content_path,
            content,
            cache_root=cache_root,
            expected_digest=digest,
            deadline=deadline,
        )
        if request_contract is not None:
            _write_cache_contract(content_path, request_contract, cache_root=cache_root)
    return content_path


def _write_partial_payloads(
    cache_dir: Path,
    payloads: dict[str, dict[str, Any]],
    *,
    required_bar_date: date | None,
    deadline: float | None = None,
    request_contract: dict[str, object] | None = None,
) -> None:
    if required_bar_date is None:
        return
    cache_root = cache_dir.resolve()
    partial_dir = cache_dir / ".partial" / required_bar_date.isoformat()
    _assert_cache_path(cache_root, partial_dir)
    partial_dir.mkdir(parents=True, exist_ok=True)
    for symbol in sorted(payloads):
        _write_immutable_payload(
            partial_dir,
            symbol,
            payloads[symbol],
            deadline=deadline,
            request_contract=request_contract,
        )


def _write_exclusive(
    path: Path,
    content: bytes,
    *,
    cache_root: Path,
    expected_digest: str | None = None,
    deadline: float | None = None,
    max_bytes: int = _MAX_PAYLOAD_BYTES,
) -> None:
    _ensure_before_deadline(deadline)
    _assert_cache_path(cache_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_WRITE_LOCK:
        if path.exists():
            try:
                existing = _bounded_file_read(path, deadline=None, max_bytes=max_bytes)
            except ValueError:
                _quarantine_corrupt_cache(path, cache_root, expected_digest=expected_digest)
                existing = None
            if existing is not None:
                if existing == content:
                    return
                if expected_digest and hashlib.sha256(existing).hexdigest() == expected_digest:
                    raise ValueError("Yahoo cache digest collision: authoritative bytes differ")
                raise ValueError(f"immutable Yahoo cache conflict: {path}")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _ensure_before_deadline(deadline)
            # The lock makes create/replace atomic for concurrent workers in
            # this process. Hard-link creation is exclusive across processes;
            # a pre-existing target is never overwritten.
            if path.exists():
                try:
                    existing = _bounded_file_read(path, deadline=None, max_bytes=max_bytes)
                except ValueError:
                    _quarantine_corrupt_cache(path, cache_root, expected_digest=expected_digest)
                    existing = None
                if existing is not None:
                    if existing == content:
                        return
                    if expected_digest and hashlib.sha256(existing).hexdigest() == expected_digest:
                        raise ValueError(
                            "Yahoo cache digest collision: authoritative bytes differ"
                        ) from None
                    raise ValueError(f"immutable Yahoo cache conflict: {path}") from None
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                try:
                    existing = _bounded_file_read(path, deadline=None, max_bytes=max_bytes)
                except ValueError:
                    _quarantine_corrupt_cache(path, cache_root, expected_digest=expected_digest)
                    existing = None
                if existing is None:
                    # The corrupt object was removed by the bounded quarantine;
                    # retry the exclusive link with the still-fsynced temp file.
                    try:
                        os.link(temporary_path, path)
                        return
                    except FileExistsError:
                        existing = _bounded_file_read(path, deadline=None, max_bytes=max_bytes)
                if existing == content:
                    return
                if expected_digest and hashlib.sha256(existing).hexdigest() == expected_digest:
                    raise ValueError(
                        "Yahoo cache digest collision: authoritative bytes differ"
                    ) from None
                raise ValueError(f"immutable Yahoo cache conflict: {path}") from None
        finally:
            temporary_path.unlink(missing_ok=True)


def _write_csv_immutable(
    dataset: MarketDataset,
    *,
    cache_root: Path,
    deadline: float | None = None,
    request_contract: dict[str, object] | None = None,
) -> Path:
    """Write a content-addressed CSV and retain a non-authoritative alias."""

    _ensure_before_deadline(deadline)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=cache_root,
        prefix=".public_yahoo_ohlcv.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        write_ohlcv_csv(dataset, temporary_path, deadline=deadline)
        with temporary_path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        content = _bounded_file_read(
            temporary_path, deadline=deadline, max_bytes=_MAX_CSV_BYTES
        )
        _ensure_before_deadline(deadline)
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    immutable_path = cache_root / f"public_yahoo_ohlcv_{digest}.csv"
    _assert_cache_path(cache_root, immutable_path)
    if immutable_path.exists():
        try:
            existing = _bounded_file_read(
                immutable_path, deadline=None, max_bytes=_MAX_CSV_BYTES
            )
        except ValueError:
            _quarantine_corrupt_cache(immutable_path, cache_root, expected_digest=digest)
            existing = None
        if existing is not None and existing != content:
            _quarantine_corrupt_cache(immutable_path, cache_root, expected_digest=digest)
    _write_exclusive(
        immutable_path,
        content,
        cache_root=cache_root,
        expected_digest=digest,
        deadline=deadline,
        max_bytes=_MAX_CSV_BYTES,
    )
    if request_contract is not None:
        _write_cache_contract(immutable_path, request_contract, cache_root=cache_root)
    alias_path = cache_root / "public_yahoo_ohlcv.csv"
    _assert_cache_path(cache_root, alias_path)
    if not alias_path.exists():
        try:
            _write_exclusive(
                alias_path,
                content,
                cache_root=cache_root,
                deadline=deadline,
                max_bytes=_MAX_CSV_BYTES,
            )
        except ValueError:
            pass
    _ensure_before_deadline(deadline)
    return immutable_path


def _request_contract(*, range_period: str, interval: str) -> dict[str, object]:
    return {
        "events": "history",
        "includePrePost": False,
        "interval": interval,
        "range": range_period,
        "schema_version": _REQUEST_CONTRACT_SCHEMA,
    }


def _cache_contract_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.contract.json")


def _write_cache_contract(
    path: Path,
    contract: dict[str, object],
    *,
    cache_root: Path,
) -> None:
    _assert_cache_path(cache_root, path)
    contract_path = _cache_contract_path(path)
    _assert_cache_path(cache_root, contract_path)
    content = _canonical_payload_bytes(contract)
    _write_exclusive(
        contract_path,
        content,
        cache_root=cache_root,
        expected_digest=hashlib.sha256(content).hexdigest(),
        max_bytes=64 * 1024,
    )


def _validate_cache_contract(
    path: Path,
    expected: dict[str, object] | None,
    *,
    required: bool,
) -> None:
    if expected is None:
        return
    contract_path = _cache_contract_path(path)
    if not contract_path.exists():
        if required:
            raise ValueError("Yahoo cache request contract is missing")
        return
    content = _bounded_file_read(contract_path, deadline=None, max_bytes=64 * 1024)
    actual = json.loads(content.decode("utf-8"))
    if actual != expected:
        raise ValueError("Yahoo cache request contract does not match the request")


def _quarantine_corrupt_cache(
    path: Path,
    cache_root: Path,
    *,
    expected_digest: str | None = None,
) -> None:
    _assert_cache_path(cache_root, path)
    quarantine_root = cache_root / ".quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    with _CACHE_WRITE_LOCK:
        if not path.exists():
            return
        try:
            original_stat = path.stat()
            size = original_stat.st_size
            max_bytes = _MAX_CSV_BYTES if path.suffix.lower() == ".csv" else _MAX_PAYLOAD_BYTES
            if size <= max_bytes:
                content = _bounded_file_read(path, deadline=None, max_bytes=max_bytes)
                digest = hashlib.sha256(content).hexdigest()
                identity = ("full", digest, size)
                if expected_digest is not None and digest == expected_digest:
                    return
            else:
                digest = _corrupt_prefix_digest(path, size)
                identity = ("prefix", digest, size)
        except FileNotFoundError:
            return
        quarantine_path = quarantine_root / f"{path.name}.{digest}.corrupt"
        _assert_cache_path(cache_root, quarantine_path)
        try:
            if quarantine_path.exists() and _corrupt_identity(quarantine_path) != identity:
                # A bounded prefix fingerprint can collide for two oversized
                # objects. Keep both byte identities quarantined without ever
                # overwriting the first inode.
                quarantine_path = quarantine_root / (
                    f"{path.name}.{digest}.{original_stat.st_ino:x}.{uuid.uuid4().hex}.corrupt"
                )
                _assert_cache_path(cache_root, quarantine_path)
            if not quarantine_path.exists():
                os.link(path, quarantine_path)
            else:
                quarantine_stat = quarantine_path.stat()
                if (
                    quarantine_stat.st_size != size
                    or _corrupt_identity(quarantine_path) != identity
                ):
                    raise ValueError("Yahoo quarantine digest collision")
            current_stat = path.stat()
            if (
                current_stat.st_dev == original_stat.st_dev
                and current_stat.st_ino == original_stat.st_ino
            ):
                path.unlink()
            elif quarantine_path.exists() and quarantine_path.stat().st_ino == original_stat.st_ino:
                quarantine_path.unlink()
        except FileExistsError:
            # Another process already quarantined the same corrupt bytes. If
            # this path still contains those verified bytes, remove only that
            # inode so the correct digest target can be recreated.
            try:
                current_stat = path.stat()
                if (
                    _corrupt_identity(path) == identity
                    and current_stat.st_ino == original_stat.st_ino
                ):
                    path.unlink()
            except FileNotFoundError:
                return
        except FileNotFoundError:
            return


def _corrupt_prefix_digest(path: Path, size: int) -> str:
    """Fingerprint an oversized object without reading beyond a bounded prefix."""

    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(_READ_CHUNK_BYTES))
    return digest.hexdigest()


def _corrupt_identity(path: Path) -> tuple[str, str, int]:
    size = path.stat().st_size
    max_bytes = _MAX_CSV_BYTES if path.suffix.lower() == ".csv" else _MAX_PAYLOAD_BYTES
    if size > max_bytes:
        return ("prefix", _corrupt_prefix_digest(path, size), size)
    content = _bounded_file_read(path, deadline=None, max_bytes=max_bytes)
    return ("full", hashlib.sha256(content).hexdigest(), size)


def _assert_cache_path(cache_root: Path, path: Path) -> None:
    root = cache_root.resolve()
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Yahoo cache path escapes configured cache root") from exc


def canonicalize_yahoo_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Validate and return a stable canonical symbol set before I/O."""

    normalized: set[str] = set()
    for raw_symbol in symbols:
        if not isinstance(raw_symbol, str):
            raise ValueError("Yahoo acquisition symbols must be strings")
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        _validate_symbol(symbol)
        normalized.add(symbol)
    return tuple(sorted(normalized))


def _provider_symbol_map(symbols: tuple[str, ...]) -> dict[str, str]:
    mapping = {symbol: symbol.replace(".", "-") for symbol in symbols}
    aliases: dict[str, str] = {}
    for canonical, provider in mapping.items():
        previous = aliases.get(provider)
        if previous is not None and previous != canonical:
            raise ValueError(
                "Yahoo provider-symbol alias collision for canonical symbols "
                f"{previous} and {canonical} ({provider})"
            )
        aliases[provider] = canonical
    return mapping


def _validate_symbol(symbol: str) -> None:
    if len(symbol) > _MAX_SYMBOL_LENGTH or _CANONICAL_SYMBOL_RE.fullmatch(symbol) is None:
        raise ValueError(
            "Yahoo acquisition requires canonical US market symbols "
            "(ASCII letters/digits with optional '.' or '-'); symbol rejected"
        )


def _validate_payload_symbol(symbol: str, payload: dict[str, Any]) -> None:
    chart = payload.get("chart")
    result_items = chart.get("result") if isinstance(chart, dict) else None
    result = result_items[0] if isinstance(result_items, list) and result_items else None
    meta = result.get("meta") if isinstance(result, dict) else None
    payload_symbol = meta.get("symbol") if isinstance(meta, dict) else None
    if payload_symbol != symbol:
        raise ValueError(
            f"Yahoo response provider-symbol identity mismatch for {symbol}; "
            "payload metadata was not an exact governed provider match"
        )


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


def _fetch_json(
    url: str,
    *,
    timeout_seconds: float,
    deadline: float | None = None,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise TimeoutError("request transport timeout exhausted")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("request transport deadline exhausted")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dawnstrike-v2-AlphaLab/1.0 research-only",
        },
    )
    open_timeout = timeout_seconds
    if deadline is not None:
        open_timeout = min(open_timeout, max(0.001, deadline - time.monotonic()))
    with open_allowlisted_url(
        request,
        timeout=open_timeout,
        allowed_hosts=("query1.finance.yahoo.com",),
    ) as response:
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("response exceeded request transport deadline")
            read_timeout = timeout_seconds if remaining is None else min(timeout_seconds, remaining)
            _set_response_read_timeout(response, read_timeout)
            read_size = min(_READ_CHUNK_BYTES, _MAX_PAYLOAD_BYTES + 1 - total_bytes)
            chunk = response.read(read_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > _MAX_PAYLOAD_BYTES:
                raise ValueError("Yahoo response exceeded maximum payload size")
            chunks.append(chunk)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("response exceeded request transport deadline")
        payload = b"".join(chunks).decode("utf-8")
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise TypeError("expected JSON object")
    return loaded


def _set_response_read_timeout(response: Any, timeout_seconds: float) -> None:
    """Cap each blocking body read when urllib exposes its underlying socket."""

    bounded_timeout = max(0.001, timeout_seconds)
    candidates = [
        getattr(response, "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
    ]
    for socket in candidates:
        setter = getattr(socket, "settimeout", None)
        if callable(setter):
            setter(bounded_timeout)
            return
