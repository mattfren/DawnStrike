"""Build canonical scanner snapshots from minute bars and metadata."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.models import (
    SNAPSHOT_COLUMNS,
    SnapshotRow,
    parse_bool,
    utc_now_iso,
    validate_required_columns,
)

MINUTE_BAR_COLUMNS = ["ticker", "timestamp", "open", "high", "low", "close", "volume"]
PREVIOUS_CLOSE_COLUMNS = ["ticker", "previous_close"]
METADATA_COLUMNS = [
    "ticker",
    "company",
    "float_shares",
    "market_cap",
    "spread_pct",
    "short_float_pct",
    "has_news",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
]


def build_snapshot(
    minute_bars_path: str | Path,
    previous_close_path: str | Path,
    metadata_path: str | Path,
    out_path: str | Path,
) -> list[SnapshotRow]:
    bars = _read_csv(minute_bars_path, MINUTE_BAR_COLUMNS, "minute bars")
    previous_close = {
        str(row["ticker"]).upper(): float(row["previous_close"])
        for row in _read_csv(previous_close_path, PREVIOUS_CLOSE_COLUMNS, "previous close")
    }
    metadata = {
        str(row["ticker"]).upper(): row
        for row in _read_csv(metadata_path, METADATA_COLUMNS, "metadata")
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for bar in bars:
        grouped.setdefault(str(bar["ticker"]).upper(), []).append(bar)
    snapshots: list[SnapshotRow] = []
    for ticker, ticker_bars in grouped.items():
        sorted_bars = sorted(ticker_bars, key=lambda row: str(row["timestamp"]))
        latest = sorted_bars[-1]
        meta = metadata.get(ticker, {})
        premarket_price = float(latest["close"])
        previous_close_value = previous_close.get(ticker, 0.0)
        premarket_volume = sum(int(float(bar["volume"])) for bar in sorted_bars)
        row = {
            "ticker": ticker,
            "company": meta.get("company", ticker),
            "premarket_price": premarket_price,
            "previous_close": previous_close_value,
            "premarket_high": max(float(bar["high"]) for bar in sorted_bars),
            "premarket_low": min(float(bar["low"]) for bar in sorted_bars),
            "premarket_volume": premarket_volume,
            "dollar_volume": premarket_price * premarket_volume,
            "gap_pct": _gap_pct(premarket_price, previous_close_value),
            "float_shares": meta.get("float_shares", ""),
            "market_cap": meta.get("market_cap", ""),
            "spread_pct": meta.get("spread_pct", 0.0),
            "short_float_pct": meta.get("short_float_pct", ""),
            "has_news": parse_bool(meta.get("has_news", False)),
            "catalyst_headline": meta.get("catalyst_headline", ""),
            "catalyst_url": meta.get("catalyst_url", ""),
            "current_halt": parse_bool(meta.get("current_halt", False)),
            "recent_offering": parse_bool(meta.get("recent_offering", False)),
            "reverse_split_90d": parse_bool(meta.get("reverse_split_90d", False)),
            "source": "csv_builder",
            "as_of_timestamp": latest.get("timestamp") or utc_now_iso(),
        }
        snapshots.append(SnapshotRow.from_mapping(row, source="snapshot_builder"))
    write_snapshot_csv(snapshots, out_path)
    return snapshots


def _gap_pct(price: float, previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    return ((price - previous_close) / previous_close) * 100


def write_snapshot_csv(rows: list[SnapshotRow], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())


def _read_csv(path: str | Path, required_columns: list[str], source: str) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise SnapshotValidationError(f"{source} file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{source} file is empty or missing a header row")
        validate_required_columns(set(reader.fieldnames), required_columns, str(csv_path))
        return list(reader)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a canonical premarket snapshot CSV")
    parser.add_argument("--minute-bars", required=True)
    parser.add_argument("--previous-close", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    rows = build_snapshot(args.minute_bars, args.previous_close, args.metadata, args.out)
    print(f"Wrote {len(rows)} snapshot row(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
