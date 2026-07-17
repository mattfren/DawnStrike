"""Benchmark observation and reporting without fake market data."""

from __future__ import annotations

import csv
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_SOURCE_QUALITY = "Unverified free web data"


def ensure_benchmark_tables(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS benchmark_observations (
                benchmark_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market_date TEXT NOT NULL,
                open_price REAL,
                close_price REAL,
                one_min_price REAL,
                five_min_price REAL,
                fifteen_min_price REAL,
                lunch_price REAL,
                source TEXT NOT NULL,
                source_quality TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS benchmark_performance (
                market_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                return_1m REAL,
                return_5m REAL,
                return_15m REAL,
                return_lunch REAL,
                return_close REAL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (market_date, symbol)
            );
            """
        )


def observe_benchmark(
    *,
    symbol: str = "SPY",
    db_path: str | Path = "data/shadow_real.sqlite",
    source: str = "yahoo",
    out_dir: str | Path = "outputs/benchmark",
    persist: bool = False,
    market_date: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    day = (market_date or date.today().isoformat())[:10]
    normalized_symbol = symbol.upper().strip()
    row: dict[str, Any] | None = None
    error = ""
    if source.lower() == "yahoo":
        try:
            row = _fetch_yahoo_daily_bar(normalized_symbol, day)
        except OSError as exc:
            error = str(exc)
    if row:
        row.update(
            {
                "benchmark_id": f"{normalized_symbol}:{day}:{source}",
                "symbol": normalized_symbol,
                "market_date": day,
                "source": source,
                "source_quality": BENCHMARK_SOURCE_QUALITY,
                "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        )
        if persist:
            persist_benchmark_observation(db_path, row)
    result = {
        "status": "observed" if row else "pending",
        "symbol": normalized_symbol,
        "market_date": day,
        "source": source,
        "source_quality": BENCHMARK_SOURCE_QUALITY,
        "observation": row or {},
        "message": "Benchmark pending" if not row else "Benchmark observation saved",
        "error": error,
    }
    (output_dir / "benchmark_observe.json").write_text(
        json.dumps(result, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def benchmark_report(
    *,
    symbol: str = "SPY",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/benchmark_report",
) -> dict[str, Any]:
    ensure_benchmark_tables(db_path)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_symbol = symbol.upper().strip()
    observations = load_benchmark_observations(db_path, normalized_symbol)
    rows = [_performance_row(row) for row in observations]
    _persist_benchmark_performance(db_path, rows)
    csv_path = output_dir / "benchmark_performance.csv"
    json_path = output_dir / "benchmark_report.json"
    md_path = output_dir / "benchmark_report.md"
    _write_csv(csv_path, rows)
    result = {
        "status": "pending" if not rows else "complete",
        "symbol": normalized_symbol,
        "db_path": str(db_path),
        "observation_count": len(observations),
        "comparison_status": "Benchmark pending" if not rows else "Benchmark available",
        "source_quality": BENCHMARK_SOURCE_QUALITY if rows else "",
        "paths": {
            "benchmark_performance": str(csv_path),
            "benchmark_report_json": str(json_path),
            "benchmark_report": str(md_path),
        },
    }
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    _write_markdown(md_path, result, rows)
    return result


def persist_benchmark_observation(db_path: str | Path, row: dict[str, Any]) -> None:
    ensure_benchmark_tables(db_path)
    payload = json.dumps(row, sort_keys=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO benchmark_observations (
                benchmark_id, symbol, market_date, open_price, close_price,
                one_min_price, five_min_price, fifteen_min_price, lunch_price,
                source, source_quality, observed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["benchmark_id"],
                row["symbol"],
                row["market_date"],
                row.get("open_price"),
                row.get("close_price"),
                row.get("one_min_price"),
                row.get("five_min_price"),
                row.get("fifteen_min_price"),
                row.get("lunch_price"),
                row["source"],
                row["source_quality"],
                row["observed_at"],
                payload,
            ),
        )


def load_benchmark_observations(db_path: str | Path, symbol: str) -> list[dict[str, Any]]:
    ensure_benchmark_tables(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM benchmark_observations
            WHERE symbol = ?
            ORDER BY market_date
            """,
            (symbol.upper(),),
        ).fetchall()
    return [dict(row) for row in rows]


def _fetch_yahoo_daily_bar(symbol: str, market_date: str) -> dict[str, Any] | None:
    period1 = int(datetime.fromisoformat(f"{market_date}T00:00:00").timestamp())
    period2 = int(datetime.fromisoformat(f"{market_date}T23:59:59").timestamp())
    query = urllib.parse.urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "includePrePost": "false",
            "events": "history",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "Dawnstrike research app"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    results = list(payload.get("chart", {}).get("result") or [])
    if not results:
        return None
    quote = dict((results[0].get("indicators", {}).get("quote") or [{}])[0])
    opens = list(quote.get("open") or [])
    closes = list(quote.get("close") or [])
    open_price = _first_number(opens)
    close_price = _last_number(closes)
    if open_price is None or close_price is None:
        return None
    return {
        "open_price": open_price,
        "close_price": close_price,
        "payload_json": {"provider": "yahoo_chart", "url": url},
    }


def _performance_row(row: dict[str, Any]) -> dict[str, Any]:
    open_price = _number(row.get("open_price"))
    return {
        "market_date": row.get("market_date"),
        "symbol": row.get("symbol"),
        "return_1m": _return_pct(open_price, row.get("one_min_price")),
        "return_5m": _return_pct(open_price, row.get("five_min_price")),
        "return_15m": _return_pct(open_price, row.get("fifteen_min_price")),
        "return_lunch": _return_pct(open_price, row.get("lunch_price")),
        "return_close": _return_pct(open_price, row.get("close_price")),
        "source": row.get("source"),
        "source_quality": row.get("source_quality"),
    }


def _persist_benchmark_performance(db_path: str | Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with sqlite3.connect(db_path) as connection:
        for row in rows:
            connection.execute(
                """
                INSERT OR REPLACE INTO benchmark_performance (
                    market_date, symbol, return_1m, return_5m, return_15m,
                    return_lunch, return_close, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["market_date"],
                    row["symbol"],
                    row.get("return_1m"),
                    row.get("return_5m"),
                    row.get("return_15m"),
                    row.get("return_lunch"),
                    row.get("return_close"),
                    json.dumps(row, sort_keys=True),
                ),
            )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "market_date",
        "symbol",
        "return_1m",
        "return_5m",
        "return_15m",
        "return_lunch",
        "return_close",
        "source",
        "source_quality",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(path: Path, result: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Benchmark Report",
        "",
        f"Status: {result['comparison_status']}",
        f"Symbol: {result['symbol']}",
        "",
    ]
    if not rows:
        lines.append("Benchmark pending. No SPY comparison is shown until real observations exist.")
    else:
        lines.append("Rows use persisted benchmark observations only.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _first_number(values: list[Any]) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _last_number(values: list[Any]) -> float | None:
    for value in reversed(values):
        number = _number(value)
        if number is not None:
            return number
    return None


def _return_pct(open_price: float | None, value: Any) -> float | None:
    price = _number(value)
    if open_price is None or open_price <= 0 or price is None:
        return None
    return round(((price - open_price) / open_price) * 100.0, 4)


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
