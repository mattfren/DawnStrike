"""Fetch and cache SPY 1-minute bars (feed=iex) for the ID08 replay window.

Read-only market-data fetch only. Never touches trading/order APIs. Credentials
are loaded from C:\\r\\dawnstrike-state\\secrets\\runtime.env into the process
environment for the request only; they are never printed, logged, or written
to any output file.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.config import load_config
from intraday_scanner.providers.alpaca_provider import AlpacaProvider

RUNTIME_ENV = Path(r"C:\r\dawnstrike-state\secrets\runtime.env")
OUTPUT_DIR = Path(r"C:\r\dsos-00-v3\id08_run")
CACHE_PATH = OUTPUT_DIR / "spy_1min_bars_cache.csv"
ET = ZoneInfo("America/New_York")

FETCH_START_DATE = date(2026, 1, 2)
FETCH_END_DATE = date(2026, 9, 18)


def _load_runtime_env_into_process(path: Path) -> None:
    """Load ONLY the Alpaca market-data credentials into os.environ.

    Every other key in runtime.env is intentionally ignored here -- this
    script has no use for Telegram/Vercel/paper-execution settings, and
    importing them would be scope creep for a read-only data fetch.
    """

    import os

    if not path.exists():
        raise SystemExit(f"runtime.env not found at {path}")
    wanted = {"ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"}
    found = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in wanted:
            os.environ[key] = value.strip().strip('"').strip("'")
            found.add(key)
    missing = wanted - found
    if missing:
        raise SystemExit(f"runtime.env is missing required key(s): {sorted(missing)}")


def main() -> int:
    _load_runtime_env_into_process(RUNTIME_ENV)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    config = load_config(env_file=OUTPUT_DIR / "__no_such_env_file__", provider="alpaca")
    if config.alpaca_data_feed != "iex":
        raise SystemExit(
            f"frozen spec requires feed=iex for both replay and live; got "
            f"{config.alpaca_data_feed!r}"
        )
    provider = AlpacaProvider(config)

    start_utc = datetime.combine(FETCH_START_DATE, time(0, 0), tzinfo=ET).astimezone(ZoneInfo("UTC"))
    end_utc = (
        datetime.combine(FETCH_END_DATE, time(23, 59), tzinfo=ET) + timedelta(minutes=1)
    ).astimezone(ZoneInfo("UTC"))
    start_iso = start_utc.isoformat().replace("+00:00", "Z")
    end_iso = end_utc.isoformat().replace("+00:00", "Z")

    print(f"Fetching SPY 1-min bars feed=iex {start_iso} .. {end_iso} (credentials not printed)")
    rows = provider.get_minute_bars(["SPY"], start_iso, end_iso, config)
    print(f"Fetched {len(rows)} raw bar rows")

    with CACHE_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "timestamp_utc", "open", "high", "low", "close", "volume"])
        for row in rows:
            writer.writerow(
                [
                    row["ticker"],
                    row["timestamp"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                ]
            )
    print(f"Cached to {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
