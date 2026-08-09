"""Offline fixture-backed intraday artifact backfill with explicit retention."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.storage.intraday_evidence_store import IntradayEvidenceStore
from intraday_scanner.v2.data_truth.intraday import (
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
)


def backfill_path_replay(
    store: IntradayEvidenceStore,
    *,
    replay: dict[str, Any],
) -> dict[str, int]:
    """Persist one registered replay receipt without touching legacy positions."""

    required = (
        "path_replay_id",
        "cohort",
        "selection_id",
        "policy_version",
        "artifact_identity",
        "artifact_hash_sha256",
    )
    missing = [field for field in required if not str(replay.get(field) or "").strip()]
    if missing:
        raise ValueError(f"path replay is missing required identity: {', '.join(missing)}")
    return store.persist_path_replay(replay)


def backfill_fixture(args: argparse.Namespace) -> dict[str, Any]:
    if not args.retention_allowed:
        raise ValueError("fixture backfill requires --retention-allowed from operator metadata")
    raw_bytes = args.input.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    normalized_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    now = datetime.now(timezone.utc)
    raw_hash = _sha256(raw_bytes)
    normalized_hash = _sha256(normalized_bytes)
    store = IntradayEvidenceStore(
        args.db_path,
        evidence_root=args.evidence_root,
        code_sha=args.code_sha,
    )
    manifest = store.store_artifact(
        provider=args.provider,
        feed=args.feed,
        artifact_kind=args.artifact_kind,
        symbol=args.symbol,
        market_date=args.market_date,
        exchange_session_id=args.exchange_session_id,
        entitlement=args.entitlement,
        request_start=args.request_start,
        request_end=args.request_end,
        fetched_at=now,
        raw_bytes=raw_bytes,
        normalized_bytes=normalized_bytes,
        retention_allowed=True,
        retention_status="retained_fixture",
    )
    source = IntradaySourceMetadata(
        provider=args.provider,
        feed=args.feed,
        entitlement=args.entitlement,
        exchange_session_id=args.exchange_session_id,
        request_start=args.request_start,
        request_end=args.request_end,
        fetched_at=now,
        code_sha=args.code_sha,
        raw_artifact_hash_sha256=raw_hash,
        normalized_artifact_hash_sha256=normalized_hash,
        retention_status="retained_fixture",
    )
    receipt = store.record_coverage(
        IntradayCoverageReceipt(
            coverage_receipt_id=_sha256(
                f"{manifest.artifact_manifest_id}|coverage".encode()
            ),
            provider=args.provider,
            feed=args.feed,
            entitlement=args.entitlement,
            symbol=args.symbol,
            market_date=args.market_date,
            exchange_session_id=args.exchange_session_id,
            request_start=args.request_start,
            request_end=args.request_end,
            status=IntradayCoverageStatus.COMPLETE,
            source_metadata=source,
            observed_start=args.request_start,
            observed_end=args.request_end,
            artifact_manifest_ids=(manifest.artifact_manifest_id,),
            created_at=now,
        )
    )
    return {
        "status": "PASS",
        "pages_verified": 1,
        "restart_cursor": None,
        "artifact_manifest_id": manifest.artifact_manifest_id,
        "coverage_receipt_id": receipt.coverage_receipt_id,
        "raw_hash": raw_hash,
        "normalized_hash": normalized_hash,
        "retention_status": "retained_fixture",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", default="fixture")
    parser.add_argument("--feed", default="fixture_consolidated")
    parser.add_argument("--artifact-kind", default="intraday-bars")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--exchange-session-id", required=True)
    parser.add_argument("--entitlement", default="operator-fixture")
    parser.add_argument("--code-sha", default="fixture")
    parser.add_argument("--retention-allowed", action="store_true")
    parser.add_argument("--request-start", type=_utc_datetime, required=True)
    parser.add_argument("--request-end", type=_utc_datetime, required=True)
    args = parser.parse_args()
    result = backfill_fixture(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


def _utc_datetime(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware UTC")
    return timestamp


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
