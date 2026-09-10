"""CLI for the bounded OPS05 delayed historical bar producer."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.config import load_config
from intraday_scanner.observation.ops05_historical_bars import produce_historical_bars
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.base import IntradayPage


class FixtureProvider:
    """Small deterministic page provider used by offline controls."""

    provider_name = "alpaca"
    feed = "sip"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: dict[str, int] = {"bars": 0, "corporate_actions": 0}

    def _page(self, endpoint: str, page_token: str | None) -> IntradayPage:
        pages = self.payload.get(endpoint)
        if not isinstance(pages, list):
            raise ValueError(f"fixture lacks {endpoint} pages")
        index = self.calls[endpoint]
        if index >= len(pages):
            raise ValueError(f"fixture exhausted for {endpoint}")
        self.calls[endpoint] = index + 1
        value = pages[index]
        if not isinstance(value, dict) or not isinstance(value.get("items", []), list):
            raise ValueError("fixture page is invalid")
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return IntradayPage(
            provider="alpaca",
            feed="sip",
            endpoint=endpoint,
            items=tuple(dict(item) for item in value["items"]),
            next_page_token=value.get("next_page_token"),
            raw_payload_hash_sha256=value.get(
                "raw_payload_hash_sha256", hashlib.sha256(raw.encode()).hexdigest()
            ),
            request_id=str(value.get("request_id") or ""),
        )

    def get_bars_page(self, symbols, start, end, config, *, page_token=None):
        return self._page("bars", page_token)

    def get_corporate_actions_page(self, symbols, start, end, config, *, page_token=None):
        return self._page("corporate_actions", page_token)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-config-hash", required=True)
    parser.add_argument("--capture-receipt-hash", required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Use the existing authenticated Alpaca SIP client; omitted by default.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    census = json.loads(args.census.read_text(encoding="utf-8"))
    if not isinstance(census, list):
        raise SystemExit("census must be a JSON list")
    if args.execute:
        config = load_config(
            env_file=args.env_file,
            provider="alpaca",
            alpaca_data_feed="sip",
            request_retries=1,
            historical_intraday_max_pages=100,
            historical_intraday_page_limit=10_000,
        )
        provider = AlpacaProvider(config)
    else:
        if args.fixture is None:
            raise SystemExit("offline mode requires --fixture; --execute is the only network path")
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        if not isinstance(fixture, dict):
            raise SystemExit("fixture must be a JSON object")
        provider = FixtureProvider(fixture)
        config = object()
    receipt = produce_historical_bars(
        market_date=args.market_date,
        census=census,
        provider=provider,
        config=config,
        output_root=args.output_root,
        source_config_hash=args.source_config_hash,
        capture_receipt_hash=args.capture_receipt_hash,
        resume_across_roots=True,
    )
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "bar_count": receipt["coverage"]["bar_count"],
                "output_root": str(args.output_root.resolve()),
                "network_enabled": args.execute,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
