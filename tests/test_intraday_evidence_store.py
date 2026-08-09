from __future__ import annotations

import gzip
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.storage.intraday_evidence_store import (
    IntradayEvidenceStore,
    RetentionNotPermittedError,
    SourceConflictError,
)
from intraday_scanner.v2.data_truth.intraday import (
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
)

UTC = timezone.utc
REQUEST_START = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
REQUEST_END = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 7, 20, 1, tzinfo=UTC)


def _store(tmp_path: Path) -> IntradayEvidenceStore:
    return IntradayEvidenceStore(
        tmp_path / "state.sqlite",
        evidence_root=tmp_path / "evidence",
        code_sha="test-code-sha",
    )


def _source(raw_hash: str, normalized_hash: str) -> IntradaySourceMetadata:
    return IntradaySourceMetadata(
        provider="provider",
        feed="bars",
        entitlement="research",
        exchange_session_id="XNYS:2026-08-07:regular",
        request_start=REQUEST_START,
        request_end=REQUEST_END,
        fetched_at=FETCHED_AT,
        code_sha="test-code-sha",
        raw_artifact_hash_sha256=raw_hash,
        normalized_artifact_hash_sha256=normalized_hash,
        retention_status="retained",
    )


def _coverage(raw_hash: str, normalized_hash: str) -> IntradayCoverageReceipt:
    return IntradayCoverageReceipt(
        coverage_receipt_id="coverage-1",
        provider="provider",
        feed="bars",
        entitlement="research",
        symbol="TST",
        market_date="2026-08-07",
        exchange_session_id="XNYS:2026-08-07:regular",
        request_start=REQUEST_START,
        request_end=REQUEST_END,
        status=IntradayCoverageStatus.COMPLETE,
        source_metadata=_source(raw_hash, normalized_hash),
    )


def test_artifacts_are_compressed_partitioned_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kwargs = {
        "provider": "provider",
        "feed": "bars",
        "artifact_kind": "intraday-bars",
        "symbol": "TST",
        "market_date": "2026-08-07",
        "exchange_session_id": "XNYS:2026-08-07:regular",
        "entitlement": "research",
        "request_start": REQUEST_START,
        "request_end": REQUEST_END,
        "fetched_at": FETCHED_AT,
        "raw_bytes": b"raw-source",
        "normalized_bytes": b"normalized-source",
        "retention_allowed": True,
    }

    first = store.store_artifact(**kwargs)
    second = store.store_artifact(**kwargs)

    assert second == first
    assert Path(first.raw_artifact_path).parts[-3:] == (
        "2026-08-07",
        "TST",
        f"raw-{first.raw_artifact_hash_sha256}.bin.gz",
    )
    assert gzip.decompress(Path(first.raw_artifact_path).read_bytes()) == b"raw-source"
    assert gzip.decompress(Path(first.normalized_artifact_path).read_bytes()) == (
        b"normalized-source"
    )


def test_same_identity_with_different_content_is_source_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    common = {
        "provider": "provider",
        "feed": "bars",
        "artifact_kind": "intraday-bars",
        "symbol": "TST",
        "market_date": "2026-08-07",
        "exchange_session_id": "XNYS:2026-08-07:regular",
        "entitlement": "research",
        "request_start": REQUEST_START,
        "request_end": REQUEST_END,
        "fetched_at": FETCHED_AT,
        "normalized_bytes": b"normalized-source",
        "retention_allowed": True,
    }
    store.store_artifact(raw_bytes=b"first", **common)

    with pytest.raises(SourceConflictError, match="SOURCE_CONFLICT|hashes differ"):
        store.store_artifact(raw_bytes=b"second", **common)


def test_retention_denied_refuses_before_database_or_file_write(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(RetentionNotPermittedError):
        store.store_artifact(
            provider="provider",
            feed="bars",
            artifact_kind="intraday-bars",
            symbol="TST",
            market_date="2026-08-07",
            exchange_session_id="XNYS:2026-08-07:regular",
            entitlement="no-retention",
            request_start=REQUEST_START,
            request_end=REQUEST_END,
            fetched_at=FETCHED_AT,
            raw_bytes=b"raw",
            normalized_bytes=b"normalized",
            retention_allowed=False,
        )

    assert not (tmp_path / "state.sqlite").exists()
    assert not (tmp_path / "evidence").exists()


def test_coverage_receipt_is_append_only_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = _coverage("raw", "normalized")

    assert store.record_coverage(receipt) == receipt
    assert store.record_coverage(receipt) == receipt

    with pytest.raises(SourceConflictError):
        store.record_coverage(_coverage("different-raw", "normalized"))
