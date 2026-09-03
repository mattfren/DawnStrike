"""Capture authenticated read-only intraday evidence with resumable checkpoints.

This command imports market-data providers only.  It has no trading SDK, order
endpoint, broker client, or mutation surface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
    symbols = _read_symbols(args.symbols_file, args.symbols_file_sha256)
    metadata = _read_metadata(
        args.operator_entitlement_metadata,
        args.operator_entitlement_metadata_sha256,
    )
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
    # A terminal receipt is not the same thing as a successful capture.  The
    # scheduler must retry or surface incomplete evidence instead of treating
    # PARTIAL/NO_DATA as success.  Keep distinct codes for the two truthful
    # terminal outcomes so callers can classify the failure without parsing
    # logs.
    return _capture_exit_code(str(receipt.get("status") or ""))


def _capture_exit_code(status: str) -> int:
    if status == "COMPLETE":
        return 0
    if status == "PARTIAL":
        return 20
    if status == "NO_DATA":
        return 21
    return 1


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
    parser.add_argument("--symbols-file-sha256", required=True)
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
    parser.add_argument("--operator-entitlement-metadata-sha256", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--include-trades", action="store_true")
    parser.add_argument("--include-quotes", action="store_true")
    parser.add_argument("--include-corporate-actions", action="store_true")
    return parser


def _provider(name: str, config: ScannerConfig) -> HistoricalIntradayProvider:
    if name == "alpaca":
        return AlpacaProvider(config)  # type: ignore[arg-type]
    return MassiveMarketDataProvider(config)  # type: ignore[arg-type]


def _read_symbols(path: Path, expected_sha256: str) -> list[str]:
    raw = _read_authenticated_bytes(
        path,
        expected_sha256,
        label="symbols file",
        max_bytes=4 * 1024 * 1024,
    )
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"symbols file is not UTF-8: {path}") from exc
    symbols: list[str] = []
    for line in text.splitlines():
        symbol = line.strip().upper()
        if symbol and not symbol.startswith("#") and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise SystemExit("symbols file must contain at least one symbol")
    return symbols


def _read_metadata(path: Path, expected_sha256: str) -> dict[str, Any]:
    raw = _read_authenticated_bytes(
        path,
        expected_sha256,
        label="operator entitlement metadata",
        max_bytes=1024 * 1024,
    )
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"operator entitlement metadata is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("operator entitlement metadata must be a JSON object")
    return value


def _read_authenticated_bytes(
    path: Path,
    expected_sha256: str,
    *,
    label: str,
    max_bytes: int,
) -> bytes:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise SystemExit(f"{label} expected SHA-256 is invalid")
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{label} is not a regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"{label} is unreadable: {path}") from exc
    if len(raw) > max_bytes:
        raise SystemExit(f"{label} exceeds its byte ceiling")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise SystemExit(f"{label} hash mismatch")
    return raw


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
