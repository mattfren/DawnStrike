"""Emit a sanitized, content-hashed historical intraday capability receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.config import load_config
from intraday_scanner.network_safety import assert_secret_not_in_text
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.base import HistoricalIntradayProvider
from intraday_scanner.providers.massive_market_data_provider import MassiveMarketDataProvider
from intraday_scanner.storage.intraday_evidence_store import IntradayEvidenceStore
from intraday_scanner.v2.data_truth.intraday import IntradayProviderCapabilityReceipt


def build_probe_receipt(
    provider: HistoricalIntradayProvider,
    *,
    config: Any,
    operator_metadata: dict[str, Any],
    symbols: tuple[str, ...],
    code_sha: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("probe clock must be UTC")
    capability = provider.capability_probe(config)
    capability = _sanitize_capability(capability)
    credential_present = bool(capability.get("credential_present"))
    retention_allowed = bool(operator_metadata.get("retention_allowed", False))
    approved_plan = bool(operator_metadata.get("approved_plan", False))
    status = "PASS"
    if provider.provider_name == "massive" and not credential_present:
        status = "BLOCKED_EXTERNAL_MARKET_DATA_ENTITLEMENT"
    elif not credential_present:
        status = "PROVIDER_CREDENTIAL_MISSING"
    elif not approved_plan:
        status = "REQUIRES_OPERATOR_APPROVAL"
    request_start = current - timedelta(days=1)
    receipt_payload = {
        "provider": provider.provider_name,
        "feed": provider.feed,
        "credential_present": credential_present,
        "entitlement_response": operator_metadata.get("entitlement_response", "unknown"),
        "earliest_available": operator_metadata.get("earliest_available", {}),
        "delayed_or_realtime": operator_metadata.get("delayed_or_realtime", "unknown"),
        "iex_vs_consolidated": operator_metadata.get("iex_vs_consolidated", "unknown"),
        "extended_hours": operator_metadata.get("extended_hours", "unknown"),
        "symbol_and_corporate_action_coverage": operator_metadata.get(
            "symbol_and_corporate_action_coverage", "unknown"
        ),
        "pagination_limits": {
            "page_limit": config.historical_intraday_page_limit,
            "max_pages": config.historical_intraday_max_pages,
        },
        "raw_data_retention_permitted": retention_allowed,
        "estimated_request_count": max(len(symbols), 1)
        * max(config.historical_intraday_max_pages, 1),
        "estimated_byte_volume": operator_metadata.get("estimated_byte_volume", "unknown"),
        "capability": capability,
        "status": status,
    }
    raw_hash = _sha256(_stable_json(receipt_payload).encode("utf-8"))
    typed = IntradayProviderCapabilityReceipt(
        capability_receipt_id=_sha256(
            f"{provider.provider_name}|{provider.feed}|{current.isoformat()}".encode()
        ),
        provider=provider.provider_name,
        feed=provider.feed,
        entitlement=str(receipt_payload["entitlement_response"]),
        requested_at=current,
        request_start=request_start,
        request_end=current,
        fetched_at=current,
        code_sha=code_sha,
        raw_artifact_hash_sha256=raw_hash,
        normalized_artifact_hash_sha256=raw_hash,
        retention_status="permitted" if retention_allowed else "not_permitted",
        capabilities=receipt_payload,
        receipt_hash_sha256=raw_hash,
    )
    result = typed.to_dict()
    result.update(
        {
            "credential_present": credential_present,
            "pagination_limits": receipt_payload["pagination_limits"],
            "raw_data_retention_permitted": retention_allowed,
            "estimated_request_count": receipt_payload["estimated_request_count"],
            "estimated_byte_volume": receipt_payload["estimated_byte_volume"],
        }
    )
    result["probe_status"] = status
    result["receipt_hash_sha256"] = _sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert_secret_not_in_text(json.dumps(result, sort_keys=True), _configured_secrets())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("alpaca", "massive"), required=True)
    parser.add_argument("--config", default=".env")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--symbols", default="AAPL")
    parser.add_argument("--code-sha", default="unknown")
    parser.add_argument("--operator-entitlement-json", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    provider: HistoricalIntradayProvider
    if args.provider == "alpaca":
        provider = AlpacaProvider(config)
    else:
        provider = MassiveMarketDataProvider(config)
    operator_metadata = {}
    if args.operator_entitlement_json:
        operator_metadata = json.loads(
            args.operator_entitlement_json.read_text(encoding="utf-8")
        )
    receipt = build_probe_receipt(
        provider,
        config=config,
        operator_metadata=operator_metadata,
        symbols=tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip()),
        code_sha=args.code_sha,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.db_path:
        store = IntradayEvidenceStore(
            args.db_path, evidence_root=config.intraday_evidence_root
        )
        store.record_provider_capability_receipt(
            IntradayProviderCapabilityReceipt.from_dict(receipt)
        )
    print(json.dumps({"status": receipt["probe_status"], "output": str(args.output)}))
    return 0


def _sanitize_capability(value: dict[str, Any]) -> dict[str, Any]:
    blocked = {"api_key", "secret_key", "authorization", "token", "password"}
    return {key: value[key] for key in value if key.lower() not in blocked}


def _configured_secrets() -> tuple[str, ...]:
    return tuple(
        value
        for name in (
            "ALPACA_API_KEY_ID",
            "ALPACA_API_SECRET_KEY",
            "MASSIVE_API_KEY",
            "POLYGON_API_KEY",
        )
        if (value := os.environ.get(name, ""))
    )


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
