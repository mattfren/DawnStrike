"""Optional read-only Yahoo Finance chart fetch/cache adapter."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import tempfile
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
_CANONICAL_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*$")
_MAX_SYMBOL_LENGTH = 16
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_CACHE_WRITE_LOCK = threading.RLock()


@dataclass(frozen=True)
class _SymbolFetchResult:
    symbol: str
    payload: dict[str, Any] | None
    attempts: int
    error: str | None = None
    from_cache: bool = False
    cache_path: Path | None = None
    completed_at: float | None = None


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
    request_gate = _RequestRateGate(max_requests_per_second)

    # The executor is deliberately bounded.  Futures are consumed in the
    # original symbol order below, so network completion order cannot change
    # warning order, CSV order, or any downstream hash.
    executor = ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(requested_symbols))))
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
            deadline=deadline,
            request_gate=request_gate,
            cache_root=cache_root,
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
        source_refs.extend(
            (
                f"canonical_symbol:{normalized}",
                f"yahoo_symbol:{provider_symbols[normalized]}",
                urls[normalized],
                raw_path.as_posix(),
            )
        )

    csv_path = _write_csv_immutable(dataset, cache_root=cache_root)
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
    provider_symbol: str,
    url: str,
    cache_dir: Path,
    timeout_seconds: float,
    max_attempts: int,
    retry_backoff_seconds: float,
    required_bar_date: date | None,
    deadline: float | None,
    request_gate: _RequestRateGate,
    cache_root: Path,
) -> _SymbolFetchResult:
    cached = _read_cached_payload(
        cache_root,
        symbol,
        provider_symbol=provider_symbol,
        required_bar_date=required_bar_date,
        cache_root=cache_root,
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
            if required_bar_date is not None and not any(
                bar.timestamp.date() == required_bar_date for bar in bars
            ):
                raise ValueError(
                    f"response lacks required completed bar {required_bar_date.isoformat()}"
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
    provider_symbol: str,
    required_bar_date: date | None,
    cache_root: Path,
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
        _assert_cache_path(cache_root, path)
        if required_bar_date is None:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            _validate_payload_symbol(provider_symbol, payload)
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
    cache_root = cache_dir.resolve()
    _validate_symbol(symbol)
    content = _canonical_payload_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()[:16]
    content_path = cache_dir / f"{symbol.lower()}_chart_{digest}.json"
    # The digest path is the sole authoritative source identity.  The old
    # ``<symbol>_chart.json`` alias is intentionally never returned: another
    # process may replace it between acquisition and DataTruth hashing.
    _assert_cache_path(cache_root, content_path)
    with _CACHE_WRITE_LOCK:
        if content_path.exists() and content_path.read_bytes() != content:
            _quarantine_corrupt_cache(content_path, cache_root)
        _write_exclusive(content_path, content, cache_root=cache_root)
    return content_path


def _write_partial_payloads(
    cache_dir: Path,
    payloads: dict[str, dict[str, Any]],
    *,
    required_bar_date: date | None,
) -> None:
    if required_bar_date is None:
        return
    cache_root = cache_dir.resolve()
    partial_dir = cache_dir / ".partial" / required_bar_date.isoformat()
    _assert_cache_path(cache_root, partial_dir)
    partial_dir.mkdir(parents=True, exist_ok=True)
    for symbol in sorted(payloads):
        _write_immutable_payload(partial_dir, symbol, payloads[symbol])


def _write_exclusive(path: Path, content: bytes, *, cache_root: Path) -> None:
    _assert_cache_path(cache_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_WRITE_LOCK:
        if path.exists():
            if path.read_bytes() == content:
                return
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
            # The lock makes create/replace atomic for concurrent workers in
            # this process; a pre-existing target is never overwritten.
            if path.exists():
                if path.read_bytes() == content:
                    return
                raise ValueError(f"immutable Yahoo cache conflict: {path}")
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _write_csv_immutable(dataset: MarketDataset, *, cache_root: Path) -> Path:
    """Write a content-addressed CSV and retain a non-authoritative alias."""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=cache_root,
        prefix=".public_yahoo_ohlcv.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        write_ohlcv_csv(dataset, temporary_path)
        with temporary_path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        content = temporary_path.read_bytes()
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    immutable_path = cache_root / f"public_yahoo_ohlcv_{digest}.csv"
    _assert_cache_path(cache_root, immutable_path)
    if immutable_path.exists() and immutable_path.read_bytes() != content:
        _quarantine_corrupt_cache(immutable_path, cache_root)
    _write_exclusive(immutable_path, content, cache_root=cache_root)
    alias_path = cache_root / "public_yahoo_ohlcv.csv"
    _assert_cache_path(cache_root, alias_path)
    if not alias_path.exists():
        try:
            _write_exclusive(alias_path, content, cache_root=cache_root)
        except ValueError:
            pass
    return immutable_path


def _quarantine_corrupt_cache(path: Path, cache_root: Path) -> None:
    _assert_cache_path(cache_root, path)
    quarantine_root = cache_root / ".quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    with _CACHE_WRITE_LOCK:
        if not path.exists():
            return
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        quarantine_path = quarantine_root / f"{path.name}.{digest}.corrupt"
        _assert_cache_path(cache_root, quarantine_path)
        if path.exists():
            if not quarantine_path.exists():
                try:
                    os.replace(path, quarantine_path)
                except FileNotFoundError:
                    # A separate process may have quarantined this exact
                    # corrupt object between the existence check and replace.
                    return
            elif path.read_bytes() == quarantine_path.read_bytes():
                path.unlink()


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
