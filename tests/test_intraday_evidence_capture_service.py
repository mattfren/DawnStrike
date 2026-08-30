from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.base import IntradayPage
from intraday_scanner.services.intraday_evidence_capture_service import (
    CaptureRequest,
    IntradayEvidenceCaptureService,
)


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
        exchange_session_id="NYSE-2026-08-07",
        request_start=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
        request_end=datetime(2026, 8, 7, 20, tzinfo=UTC),
        db_path=tmp_path / "capture.sqlite",
        evidence_root=tmp_path / "evidence",
        run_root=tmp_path / "runs",
        code_sha="6b00a7cfacad2e9017d6579c8e7baae7d639561d",
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
