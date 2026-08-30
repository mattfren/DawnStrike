"""Capture authenticated read-only intraday evidence with resumable checkpoints.

This command imports market-data providers only.  It has no trading SDK, order
endpoint, broker client, or mutation surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.base import HistoricalIntradayProvider
from intraday_scanner.providers.massive_market_data_provider import MassiveMarketDataProvider
from intraday_scanner.services.intraday_evidence_capture_service import (
    CaptureRequest,
    IntradayEvidenceCaptureService,
)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    symbols = _read_symbols(args.symbols_file)
    metadata = _read_metadata(args.operator_entitlement_metadata)
    request_start = _utc_datetime(args.utc_start)
    request_end = _utc_datetime(args.utc_end)
    config_overrides = {
        "provider": args.provider,
        "database_path": args.db_path,
        "intraday_evidence_root": args.evidence_root,
    }
    if args.provider == "alpaca":
        config_overrides["alpaca_data_feed"] = args.feed
    config = load_config(args.env_file, **config_overrides)
    provider = _provider(args.provider, config)
    request = CaptureRequest(
        provider=args.provider,
        feed=args.feed,
        evidence_mode=args.evidence_mode,
        symbols=tuple(symbols),
        market_date=args.market_date,
        exchange_session_id=args.exchange_session_id,
        request_start=request_start,
        request_end=request_end,
        db_path=args.db_path,
        evidence_root=args.evidence_root,
        run_root=args.run_root,
        code_sha=args.code_sha,
        source_config_hash=args.source_config_hash,
        operator_entitlement_metadata=metadata,
        include_trades=args.include_trades,
        include_quotes=args.include_quotes,
        include_corporate_actions=args.include_corporate_actions,
    )
    receipt = IntradayEvidenceCaptureService(provider, config).capture(request)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] in {"COMPLETE", "NO_DATA", "PARTIAL"} else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("alpaca", "massive"), required=True)
    parser.add_argument("--feed", required=True, help="Provider feed identity; never substituted")
    parser.add_argument(
        "--evidence-mode",
        choices=("forward_observed", "retrospective_research"),
        required=True,
    )
    parser.add_argument("--symbols-file", type=Path, required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--exchange-session-id", required=True)
    parser.add_argument(
        "--utc-start", required=True, help="Inclusive timezone-aware UTC ISO timestamp"
    )
    parser.add_argument(
        "--utc-end", required=True, help="Exclusive timezone-aware UTC ISO timestamp"
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--source-config-hash", required=True)
    parser.add_argument("--operator-entitlement-metadata", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--include-trades", action="store_true")
    parser.add_argument("--include-quotes", action="store_true")
    parser.add_argument("--include-corporate-actions", action="store_true")
    return parser


def _provider(name: str, config: ScannerConfig) -> HistoricalIntradayProvider:
    if name == "alpaca":
        return AlpacaProvider(config)  # type: ignore[arg-type]
    return MassiveMarketDataProvider(config)  # type: ignore[arg-type]


def _read_symbols(path: Path) -> list[str]:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"symbols file is not a regular file: {path}")
    symbols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        symbol = line.strip().upper()
        if symbol and not symbol.startswith("#") and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise SystemExit("symbols file must contain at least one symbol")
    return symbols


def _read_metadata(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"operator entitlement metadata is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"operator entitlement metadata is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("operator entitlement metadata must be a JSON object")
    return value


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"UTC timestamp is invalid: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise SystemExit(f"UTC timestamp must include a UTC offset: {value}")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
