"""Read-only OHLCV loading, validation, and v2 snapshot mapping."""

from __future__ import annotations

import csv
import math
import re
import time as clock
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from intraday_scanner.v2.contracts import (
    AssetClass,
    Bar,
    BarBatch,
    DataSnapshot,
    DataSourceId,
    DataValidationIssue,
    DataValidationReport,
    DataValidationSeverity,
    DataValidationStatus,
    Symbol,
    Timeframe,
)

MAX_MARKET_VOLUME = (1 << 63) - 1
MAX_MARKET_CSV_BYTES = 128 * 1024 * 1024
_INTEGER_TEXT_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class MarketBar:
    """Backward-compatible daily/market bar carrier.

    Intraday evidence uses the stricter ``IntradayBar`` contract.  These
    optional fields let existing read-only loaders carry the same lineage
    hints without changing their required constructor surface.
    """

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
    exchange_session_id: str | None = None
    price_adjustment_basis: str = "unknown"


@dataclass(frozen=True)
class MarketDataset:
    dataset_id: str
    source_kind: str
    timeframe: str
    bars_by_symbol: dict[str, tuple[MarketBar, ...]]
    source_path: str | None = None
    warnings: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.bars_by_symbol))

    @property
    def latest_timestamp(self) -> datetime | None:
        timestamps = [bars[-1].timestamp for bars in self.bars_by_symbol.values() if bars]
        return max(timestamps) if timestamps else None

    @property
    def total_bars(self) -> int:
        return sum(len(bars) for bars in self.bars_by_symbol.values())


@dataclass(frozen=True)
class ValidationResult:
    dataset_id: str
    passed: bool
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


def discover_ohlcv_csvs(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    candidates: list[Path] = []
    for path in root.rglob("*.csv"):
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle), [])
        except (OSError, StopIteration, UnicodeDecodeError):
            continue
        normalized = {column.strip().lower() for column in header}
        if {"timestamp", "open", "high", "low", "close", "volume"}.issubset(normalized):
            candidates.append(path)
    return tuple(sorted(candidates))


def load_ohlcv_csv(
    path: Path, *, dataset_id: str, source_kind: str, timeframe: str
) -> MarketDataset:
    if path.stat().st_size > MAX_MARKET_CSV_BYTES:
        raise ValueError("OHLCV CSV exceeds governed payload size")
    rows_by_symbol: dict[str, list[MarketBar]] = {}
    warnings: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_index, row in enumerate(reader, start=2):
            symbol = (row.get("symbol") or row.get("ticker") or "").strip().upper()
            timestamp_raw = (
                row.get("timestamp") or row.get("time") or row.get("date") or ""
            ).strip()
            if not symbol or not timestamp_raw:
                warnings.append(f"row {row_index}: missing symbol or timestamp")
                continue
            try:
                timestamp = _parse_timestamp(timestamp_raw)
                open_price = float(row["open"])
                high_price = float(row["high"])
                low_price = float(row["low"])
                close_price = float(row["close"])
                volume_raw = (row.get("volume") or "").strip()
                if not _INTEGER_TEXT_RE.fullmatch(volume_raw):
                    raise ValueError("volume must be a nonnegative integer")
                volume = int(volume_raw)
                if volume > MAX_MARKET_VOLUME:
                    raise ValueError("volume exceeds governed integer bound")
                if not all(
                    math.isfinite(value)
                    for value in (open_price, high_price, low_price, close_price)
                ):
                    raise ValueError("OHLC must be finite")
                if min(open_price, high_price, low_price, close_price) <= 0:
                    raise ValueError("OHLC must be positive")
                bar = MarketBar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                    vwap=_optional_float(row.get("vwap")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"row {row_index}: invalid OHLCV row ({exc})")
                continue
            rows_by_symbol.setdefault(symbol, []).append(bar)

    bars_by_symbol = {
        symbol: tuple(sorted(bars, key=lambda bar: bar.timestamp))
        for symbol, bars in rows_by_symbol.items()
    }
    return MarketDataset(
        dataset_id=dataset_id,
        source_kind=source_kind,
        timeframe=timeframe,
        bars_by_symbol=bars_by_symbol,
        source_path=str(path),
        warnings=tuple(warnings),
    )


def write_ohlcv_csv(
    dataset: MarketDataset,
    path: Path,
    *,
    deadline: float | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "vwap",
            ),
        )
        writer.writeheader()
        for symbol in dataset.symbols:
            for bar in dataset.bars_by_symbol[symbol]:
                if deadline is not None and clock.monotonic() >= deadline:
                    raise TimeoutError("OHLCV CSV generation exceeded acquisition deadline")
                values = (bar.open, bar.high, bar.low, bar.close)
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("OHLC must be finite")
                if bar.vwap is not None and not math.isfinite(bar.vwap):
                    raise ValueError("VWAP must be finite when present")
                writer.writerow(
                    {
                        "symbol": bar.symbol,
                        "timestamp": bar.timestamp.isoformat(),
                        "open": _format_float(bar.open),
                        "high": _format_float(bar.high),
                        "low": _format_float(bar.low),
                        "close": _format_float(bar.close),
                        "volume": str(bar.volume),
                        "vwap": "" if bar.vwap is None else _format_float(bar.vwap),
                    }
                )


def validate_dataset(
    dataset: MarketDataset,
    *,
    min_bars_per_symbol: int,
    max_staleness_days: int,
    as_of: datetime,
) -> ValidationResult:
    issues: list[str] = []
    warnings = list(dataset.warnings)
    if not dataset.bars_by_symbol:
        issues.append("dataset contains no accepted OHLCV bars")

    for symbol, bars in sorted(dataset.bars_by_symbol.items()):
        if len(bars) < min_bars_per_symbol:
            warnings.append(
                f"{symbol}: only {len(bars)} bars; minimum requested is {min_bars_per_symbol}"
            )
        seen: set[datetime] = set()
        previous_timestamp: datetime | None = None
        for bar in bars:
            if bar.timestamp in seen:
                issues.append(f"{symbol}: duplicate timestamp {bar.timestamp.isoformat()}")
            seen.add(bar.timestamp)
            if previous_timestamp and bar.timestamp < previous_timestamp:
                issues.append(f"{symbol}: bars are not sorted")
            previous_timestamp = bar.timestamp
            if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close)):
                issues.append(f"{symbol}: non-finite OHLC value at {bar.timestamp.isoformat()}")
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                issues.append(f"{symbol}: non-positive OHLC value at {bar.timestamp.isoformat()}")
            if bar.high < max(bar.open, bar.close, bar.low):
                issues.append(
                    f"{symbol}: high is below open/close/low at {bar.timestamp.isoformat()}"
                )
            if bar.low > min(bar.open, bar.close, bar.high):
                issues.append(
                    f"{symbol}: low is above open/close/high at {bar.timestamp.isoformat()}"
                )
            if type(bar.volume) is not int or bar.volume < 0 or bar.volume > MAX_MARKET_VOLUME:
                issues.append(f"{symbol}: invalid volume at {bar.timestamp.isoformat()}")

        latest = bars[-1].timestamp if bars else None
        if latest:
            age_days = max((as_of - latest).days, 0)
            if age_days > max_staleness_days:
                warnings.append(f"{symbol}: latest bar is {age_days} days old")

    issues.extend(timestamp_alignment_issues(dataset))

    return ValidationResult(
        dataset_id=dataset.dataset_id,
        passed=not issues,
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def has_minimum_history(dataset: MarketDataset, min_bars_per_symbol: int, min_symbols: int) -> bool:
    eligible = [
        symbol
        for symbol, bars in dataset.bars_by_symbol.items()
        if len(bars) >= min_bars_per_symbol
    ]
    return len(eligible) >= min_symbols


def timestamp_alignment_issues(dataset: MarketDataset) -> tuple[str, ...]:
    """Return issues when multi-symbol bars do not share the same timestamps."""

    symbols = dataset.symbols
    if len(symbols) <= 1:
        return ()
    reference_symbol = symbols[0]
    reference = {bar.timestamp for bar in dataset.bars_by_symbol[reference_symbol]}
    issues: list[str] = []
    for symbol in symbols[1:]:
        current = {bar.timestamp for bar in dataset.bars_by_symbol[symbol]}
        missing = reference - current
        extra = current - reference
        if missing or extra:
            issues.append(
                f"{symbol}: timestamp calendar is not aligned with {reference_symbol} "
                f"({len(missing)} missing, {len(extra)} extra)"
            )
    return tuple(issues)


def is_timestamp_aligned(dataset: MarketDataset) -> bool:
    return not timestamp_alignment_issues(dataset)


def filter_incomplete_daily_bars(
    dataset: MarketDataset,
    *,
    as_of: datetime,
    market_tz: str = "America/New_York",
) -> MarketDataset:
    """Exclude same-day public daily bars until the regular session is complete."""

    if dataset.timeframe != "1d":
        return dataset
    timezone_info = ZoneInfo(market_tz)
    as_of_local = as_of.astimezone(timezone_info)
    completed_date = as_of_local.date()
    if as_of_local.time() < time(16, 15):
        completed_date -= timedelta(days=1)
    while completed_date.weekday() >= 5:
        completed_date -= timedelta(days=1)

    accepted: dict[str, tuple[MarketBar, ...]] = {}
    excluded_count = 0
    for symbol, bars in dataset.bars_by_symbol.items():
        symbol_bars: list[MarketBar] = []
        for bar in bars:
            bar_date = bar.timestamp.astimezone(timezone_info).date()
            if bar_date <= completed_date:
                symbol_bars.append(bar)
            else:
                excluded_count += 1
        accepted[symbol] = tuple(symbol_bars)
    warnings = list(dataset.warnings)
    if excluded_count:
        warnings.append(
            f"excluded {excluded_count} incomplete or future daily bar(s) after "
            f"{completed_date.isoformat()}"
        )
    return MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol=accepted,
        source_path=dataset.source_path,
        warnings=tuple(dict.fromkeys(warnings)),
        source_refs=dataset.source_refs,
    )


def dataset_to_snapshot(
    dataset: MarketDataset,
    validation: ValidationResult,
    *,
    created_at: datetime,
) -> tuple[DataSnapshot, DataValidationReport]:
    source_id = DataSourceId(dataset.dataset_id)
    symbols = tuple(Symbol(symbol, AssetClass.EQUITY) for symbol in dataset.symbols)
    batches: list[BarBatch] = []
    for symbol in dataset.symbols:
        symbol_contract = Symbol(symbol, AssetClass.EQUITY)
        bars = tuple(
            Bar(
                symbol=symbol_contract,
                timeframe=Timeframe.DAILY,
                timestamp=bar.timestamp,
                open_price=Decimal(str(bar.open)),
                high_price=Decimal(str(bar.high)),
                low_price=Decimal(str(bar.low)),
                close_price=Decimal(str(bar.close)),
                volume=bar.volume,
                source_id=source_id,
            )
            for bar in dataset.bars_by_symbol[symbol]
        )
        batches.append(
            BarBatch(
                batch_id=f"{dataset.dataset_id}:{symbol}",
                source_id=source_id,
                symbol=symbol_contract,
                asset_class=AssetClass.EQUITY,
                timeframe=Timeframe.DAILY,
                bars=bars,
                created_at=created_at,
            )
        )

    snapshot = DataSnapshot(
        snapshot_id=f"{dataset.dataset_id}:snapshot",
        source_id=source_id,
        created_at=created_at,
        as_of=dataset.latest_timestamp or created_at,
        asset_class=AssetClass.EQUITY,
        timeframe=Timeframe.DAILY,
        symbols=symbols,
        batches=tuple(batches),
        warnings=dataset.warnings + validation.warnings,
    )
    issues = [
        DataValidationIssue(
            issue_id=f"{dataset.dataset_id}:issue:{index}",
            severity=DataValidationSeverity.ERROR,
            code="DATA_VALIDATION_ERROR",
            message=message,
            source_id=source_id,
        )
        for index, message in enumerate(validation.issues, start=1)
    ]
    warning_issues = [
        DataValidationIssue(
            issue_id=f"{dataset.dataset_id}:warning:{index}",
            severity=DataValidationSeverity.WARNING,
            code="DATA_VALIDATION_WARNING",
            message=message,
            source_id=source_id,
        )
        for index, message in enumerate(validation.warnings, start=1)
    ]
    status = DataValidationStatus.PASSED
    if issues:
        status = DataValidationStatus.FAILED
    elif warning_issues:
        status = DataValidationStatus.PASSED_WITH_WARNINGS
    report = DataValidationReport(
        report_id=f"{dataset.dataset_id}:validation",
        snapshot_id=snapshot.snapshot_id,
        source_id=source_id,
        created_at=created_at,
        status=status,
        issues=tuple(issues + warning_issues),
    )
    return snapshot, report


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    timestamp = datetime.fromisoformat(normalized)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("optional value must be finite")
    return parsed


def _format_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("market float must be finite")
    # repr(float) is deterministic and round-trips the exact IEEE-754 value;
    # fixed four-decimal formatting silently changed source prices.
    return repr(float(value))
