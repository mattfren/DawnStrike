from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import StorageError
from intraday_scanner.providers.base import IntradayPage
from intraday_scanner.services.intraday_evidence_capture_service import (
    CaptureRequest,
    IntradayEvidenceCaptureService,
)
from intraday_scanner.storage.intraday_evidence_store import IntradayEvidenceStore


class _FakeProvider:
    provider_name = "fake"
    feed = "fake_consolidated"

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def get_bars_page(self, symbols, start, end, config, *, page_token=None):
        self.calls.append(page_token)
        if page_token is None:
            return IntradayPage(
                provider=self.provider_name,
                feed=self.feed,
                endpoint="bars",
                items=(
                    {"S": "NOVA", "t": "2026-08-07T13:30:00Z", "c": 10},
                ),
                next_page_token="next",
                raw_payload_hash_sha256="a" * 64,
            )
        return IntradayPage(
            provider=self.provider_name,
            feed=self.feed,
            endpoint="bars",
            items=({"S": "NOVA", "t": "2026-08-07T13:31:00Z", "c": 11},),
            next_page_token=None,
            raw_payload_hash_sha256="b" * 64,
        )

    def get_trades_page(self, *args, **kwargs):
        raise AssertionError("optional endpoint should not be called")

    def get_quotes_page(self, *args, **kwargs):
        raise AssertionError("optional endpoint should not be called")

    def get_corporate_actions_page(self, *args, **kwargs):
        raise AssertionError("optional endpoint should not be called")


def _request(tmp_path: Path) -> CaptureRequest:
    return CaptureRequest(
        provider="fake",
        feed="fake_consolidated",
        evidence_mode="retrospective_research",
        symbols=("NOVA",),
        market_date="2026-08-07",
        exchange_session_id="XNYS:2026-08-07:regular",
        request_start=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
        request_end=datetime(2026, 8, 7, 20, tzinfo=UTC),
        db_path=tmp_path / "capture.sqlite",
        evidence_root=tmp_path / "evidence",
        run_root=tmp_path / "runs",
        code_sha="6b00a7cfacad2e9017d6579c8e7baae7d639561d",  # pragma: allowlist secret
        source_config_hash="c" * 64,
        operator_entitlement_metadata={"entitlement": "test-sip", "receipt": "op-1"},
    )


def test_capture_walks_pages_and_writes_immutable_receipt(tmp_path: Path) -> None:
    provider = _FakeProvider()
    request = _request(tmp_path)
    receipt = IntradayEvidenceCaptureService(
        provider,
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)

    assert receipt["status"] == "COMPLETE"
    assert receipt["coverage"][0]["status"] == "COMPLETE"
    assert provider.calls == [None, "next"]
    run_files = list((tmp_path / "runs").rglob("capture_run_receipt.json"))
    assert len(run_files) == 1
    payload = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert payload["evidence_mode"] == "retrospective_research"
    assert payload["source_config_hash"] == "c" * 64
    assert payload["operator_entitlement_metadata"]["entitlement"] == "test-sip"
    assert receipt["coverage"][0]["source_metadata"]["retention_status"] == "retained"
    store = IntradayEvidenceStore(request.db_path)
    assert store.load_capture_runs() == [receipt]
    assert store.persist_capture_run(receipt) is False


def test_capture_resumes_from_checkpoint_without_refetching_first_page(tmp_path: Path) -> None:
    request = _request(tmp_path)
    first = _FakeProvider()
    service = IntradayEvidenceCaptureService(
        first,
        ScannerConfig(request_retries=1, historical_intraday_max_pages=1),
    )
    first_receipt = service.capture(request)
    assert first_receipt["status"] == "PARTIAL_MISSING_INTERVALS"
    assert first.calls == [None]

    second = _FakeProvider()
    second_receipt = IntradayEvidenceCaptureService(
        second,
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    assert second_receipt["status"] == "COMPLETE"
    assert second.calls == ["next"]
    page_two = next((tmp_path / "runs").rglob("page-000001.json"))
    assert json.loads(page_two.read_text(encoding="utf-8"))[
        "previous_page_hash_sha256"
    ] == "a" * 64


class _InterruptingProvider(_FakeProvider):
    def get_bars_page(self, symbols, start, end, config, *, page_token=None):
        if page_token == "next":
            raise KeyboardInterrupt
        return super().get_bars_page(
            symbols,
            start,
            end,
            config,
            page_token=page_token,
        )


def test_capture_checkpoints_each_page_before_process_interruption(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        IntradayEvidenceCaptureService(
            _InterruptingProvider(),
            ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
        ).capture(request)

    resumed = _FakeProvider()
    receipt = IntradayEvidenceCaptureService(
        resumed,
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    assert receipt["status"] == "COMPLETE"
    assert resumed.calls == ["next"]


class _IncompleteMicrostructureProvider(_FakeProvider):
    def _page(self, endpoint: str, *, items=()):
        return IntradayPage(
            provider=self.provider_name,
            feed=self.feed,
            endpoint=endpoint,
            items=items,
            next_page_token=None,
            raw_payload_hash_sha256={
                "trades": "c" * 64,
                "quotes": "d" * 64,
                "corporate_actions": "e" * 64,
            }[endpoint],
        )

    def get_trades_page(self, *args, **kwargs):
        return self._page(
            "trades", items=({"S": "NOVA", "t": "2026-08-07T13:30:01Z", "p": 10},)
        )

    def get_quotes_page(self, *args, **kwargs):
        raise RuntimeError("quote history unavailable")

    def get_corporate_actions_page(self, *args, **kwargs):
        return self._page("corporate_actions")


def test_capture_cannot_report_complete_when_required_quotes_are_missing(tmp_path: Path) -> None:
    request = CaptureRequest(
        **{
            **_request(tmp_path).__dict__,
            "include_trades": True,
            "include_quotes": True,
            "include_corporate_actions": True,
        }
    )
    receipt = IntradayEvidenceCaptureService(
        _IncompleteMicrostructureProvider(),
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)

    assert receipt["status"] == "PARTIAL_MISSING_INTERVALS"
    endpoints = {
        row["endpoint"]: row for row in receipt["coverage"][0]["endpoint_coverage"]
    }
    assert endpoints["trades"]["status"] == "COMPLETE"
    assert endpoints["quotes"]["status"] == "PARTIAL_MISSING_INTERVALS"
    assert endpoints["corporate_actions"]["status"] == "NO_DATA"


class _NoCorporateActionProvider(_FakeProvider):
    def get_corporate_actions_page(self, *args, **kwargs):
        return IntradayPage(
            provider=self.provider_name,
            feed=self.feed,
            endpoint="corporate_actions",
            items=(),
            next_page_token=None,
            raw_payload_hash_sha256="e" * 64,
        )


def test_corporate_action_no_data_can_coexist_with_complete_run(tmp_path: Path) -> None:
    request = CaptureRequest(
        **{**_request(tmp_path).__dict__, "include_corporate_actions": True}
    )
    receipt = IntradayEvidenceCaptureService(
        _NoCorporateActionProvider(),
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)

    assert receipt["status"] == "COMPLETE"
    endpoints = {
        row["endpoint"]: row for row in receipt["coverage"][0]["endpoint_coverage"]
    }
    assert endpoints["bars"]["status"] == "COMPLETE"
    assert endpoints["corporate_actions"]["status"] == "NO_DATA"
    assert {item["endpoint"] for item in receipt["artifact_identity"]["items"]} == {
        "bars",
        "corporate_actions",
    }


def _resign_capture_receipt(receipt: dict, *, artifact_item: dict) -> dict:
    changed = json.loads(json.dumps(receipt))
    endpoint = next(
        row
        for row in changed["coverage"][0]["endpoint_coverage"]
        if row.get("artifact_manifest_id")
    )
    endpoint.update(
        artifact_manifest_id=artifact_item["artifact_manifest_id"],
        raw_artifact_hash_sha256=artifact_item["raw_artifact_hash_sha256"],
        normalized_artifact_hash_sha256=artifact_item["normalized_artifact_hash_sha256"],
    )
    changed["artifact_identity"]["items"] = [artifact_item]
    changed["artifact_identity"]["sha256"] = hashlib.sha256(
        json.dumps(
            {"items": changed["artifact_identity"]["items"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    changed["raw_artifact_hash_sha256"] = hashlib.sha256(
        json.dumps(
            [
                {
                    "endpoint": artifact_item["endpoint"],
                    "hash": artifact_item["raw_artifact_hash_sha256"],
                    "symbol": artifact_item["symbol"],
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    changed["normalized_artifact_hash_sha256"] = hashlib.sha256(
        json.dumps(
            [
                {
                    "endpoint": artifact_item["endpoint"],
                    "hash": artifact_item["normalized_artifact_hash_sha256"],
                    "symbol": artifact_item["symbol"],
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    changed["receipt_hash_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in changed.items() if key != "receipt_hash_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return changed


def _resign_receipt(receipt: dict) -> dict:
    changed = json.loads(json.dumps(receipt))
    items = changed["artifact_identity"]["items"]
    changed["artifact_identity"]["sha256"] = hashlib.sha256(
        json.dumps({"items": items}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if items:
        changed["raw_artifact_hash_sha256"] = hashlib.sha256(
            json.dumps(
                [
                    {
                        "endpoint": item["endpoint"],
                        "hash": item["raw_artifact_hash_sha256"],
                        "symbol": item["symbol"],
                    }
                    for item in items
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        changed["normalized_artifact_hash_sha256"] = hashlib.sha256(
            json.dumps(
                [
                    {
                        "endpoint": item["endpoint"],
                        "hash": item["normalized_artifact_hash_sha256"],
                        "symbol": item["symbol"],
                    }
                    for item in items
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    else:
        changed["raw_artifact_hash_sha256"] = None
        changed["normalized_artifact_hash_sha256"] = None
    changed["receipt_hash_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in changed.items() if key != "receipt_hash_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return changed


def _complete_receipt(tmp_path: Path) -> tuple[CaptureRequest, dict]:
    request = _request(tmp_path)
    receipt = IntradayEvidenceCaptureService(
        _FakeProvider(),
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    return request, receipt


def test_capture_run_rejects_nonexistent_manifest_even_with_valid_receipt_hash(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = IntradayEvidenceCaptureService(
        _FakeProvider(),
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    item = dict(receipt["artifact_identity"]["items"][0])
    item["artifact_manifest_id"] = "f" * 64

    with pytest.raises(StorageError, match="manifest does not exist"):
        IntradayEvidenceStore(request.db_path).persist_capture_run(
            _resign_capture_receipt(receipt, artifact_item=item)
        )


def test_capture_run_rejects_artifact_identity_with_wrong_endpoint(tmp_path: Path) -> None:
    request = _request(tmp_path)
    receipt = IntradayEvidenceCaptureService(
        _FakeProvider(),
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    item = dict(receipt["artifact_identity"]["items"][0])
    item["endpoint"] = "trades"

    with pytest.raises(StorageError, match="does not match endpoint coverage"):
        IntradayEvidenceStore(request.db_path).persist_capture_run(
            _resign_capture_receipt(receipt, artifact_item=item)
        )


def test_complete_capture_rejects_omitted_symbol(tmp_path: Path) -> None:
    request, receipt = _complete_receipt(tmp_path)
    changed = _resign_receipt(receipt)
    changed.pop("symbols")
    changed = _resign_receipt(changed)

    with pytest.raises(StorageError, match="symbols are missing or invalid"):
        IntradayEvidenceStore(request.db_path).persist_capture_run(changed)


def test_complete_capture_rejects_partial_endpoint(tmp_path: Path) -> None:
    request, receipt = _complete_receipt(tmp_path)
    changed = _resign_receipt(receipt)
    changed["coverage"][0]["endpoint_coverage"][0]["status"] = "PARTIAL_MISSING_INTERVALS"
    changed = _resign_receipt(changed)

    with pytest.raises(StorageError, match="incomplete endpoint coverage"):
        IntradayEvidenceStore(request.db_path).persist_capture_run(changed)


def test_complete_capture_rejects_empty_artifact_identity(tmp_path: Path) -> None:
    request, receipt = _complete_receipt(tmp_path)
    changed = _resign_receipt(receipt)
    changed["artifact_identity"]["items"] = []
    changed = _resign_receipt(changed)

    with pytest.raises(StorageError, match="artifact identity is empty"):
        IntradayEvidenceStore(request.db_path).persist_capture_run(changed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("code_sha", "7" * 40, "code_sha is not bound"),
        ("source_config_hash", "8" * 64, "source_config_hash is not bound"),
    ],
)
def test_capture_rejects_manifest_lineage_mismatch(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    request, receipt = _complete_receipt(tmp_path)
    changed = _resign_receipt(receipt)
    changed[field] = value
    changed = _resign_receipt(changed)

    with pytest.raises(StorageError, match=message):
        IntradayEvidenceStore(request.db_path).persist_capture_run(changed)


def test_capture_request_rejects_noncanonical_session_identity(tmp_path: Path) -> None:
    request = CaptureRequest(
        **{**_request(tmp_path).__dict__, "exchange_session_id": "NYSE-2026-08-07"}
    )
    with pytest.raises(ValueError, match="canonical XNYS"):
        request.validate()


def test_capture_rejects_tampered_checkpoint_page_artifact(tmp_path: Path) -> None:
    request = _request(tmp_path)
    first = _FakeProvider()
    IntradayEvidenceCaptureService(
        first,
        ScannerConfig(request_retries=1, historical_intraday_max_pages=1),
    ).capture(request)
    page_one = next((tmp_path / "runs").rglob("page-000000.json"))
    envelope = json.loads(page_one.read_text(encoding="utf-8"))
    envelope["items"][0]["c"] = 999
    page_one.write_text(json.dumps(envelope), encoding="utf-8")

    resumed = _FakeProvider()
    receipt = IntradayEvidenceCaptureService(
        resumed,
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    assert receipt["status"] == "HASH_MISMATCH"
    assert resumed.calls == []


def test_capture_requires_exact_source_and_entitlement_identity(tmp_path: Path) -> None:
    request = _request(tmp_path)
    invalid_hash = CaptureRequest(
        **{**request.__dict__, "source_config_hash": "config-hash"}
    )
    with pytest.raises(ValueError, match="source_config_hash"):
        invalid_hash.validate()

    invalid_entitlement = CaptureRequest(
        **{**request.__dict__, "operator_entitlement_metadata": {"entitlement": "sip"}}
    )
    with pytest.raises(ValueError, match="receipt/proof_id"):
        invalid_entitlement.validate()


def test_capture_run_rejects_same_run_id_with_changed_evidence(tmp_path: Path) -> None:
    request = _request(tmp_path)
    receipt = IntradayEvidenceCaptureService(
        _FakeProvider(),
        ScannerConfig(request_retries=1, historical_intraday_max_pages=4),
    ).capture(request)
    changed = dict(receipt)
    changed["status"] = "PARTIAL"
    changed["receipt_hash_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in changed.items() if key != "receipt_hash_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(StorageError, match="identity conflicts"):
        IntradayEvidenceStore(request.db_path).persist_capture_run(changed)
