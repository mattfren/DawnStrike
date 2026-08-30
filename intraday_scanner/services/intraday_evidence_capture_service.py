"""Restartable, read-only capture of provider intraday market evidence.

The service deliberately deals in provider pages and source receipts only.  It
does not construct signals, orders, fills, or strategy outcomes.  Progress is
checkpointed in a run directory, while the existing :class:`IntradayEvidenceStore`
retains immutable compressed page and aggregate artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.base import HistoricalIntradayProvider, IntradayPage
from intraday_scanner.storage.intraday_evidence_store import (
    IntradayEvidenceStore,
    SourceConflictError,
)
from intraday_scanner.v2.data_truth.intraday import (
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VALID_MODES = {"forward_observed", "retrospective_research"}
_ENDPOINTS = {
    "bars": "get_bars_page",
    "trades": "get_trades_page",
    "quotes": "get_quotes_page",
    "corporate_actions": "get_corporate_actions_page",
}


class CaptureContractError(ValueError):
    """Raised when a capture request or provider page violates its contract."""


@dataclass(frozen=True)
class CaptureRequest:
    """Immutable identity of one provider/window capture run."""

    provider: str
    feed: str
    evidence_mode: str
    symbols: tuple[str, ...]
    market_date: str
    exchange_session_id: str
    request_start: datetime
    request_end: datetime
    db_path: Path
    evidence_root: Path
    run_root: Path
    code_sha: str
    source_config_hash: str
    operator_entitlement_metadata: dict[str, Any]
    include_trades: bool = False
    include_quotes: bool = False
    include_corporate_actions: bool = False

    def validate(self) -> None:
        if self.provider.strip() == "" or self.feed.strip() == "":
            raise CaptureContractError("provider and feed are required")
        if self.evidence_mode not in _VALID_MODES:
            raise CaptureContractError(
                "evidence_mode must be forward_observed or retrospective_research"
            )
        if not self.symbols:
            raise CaptureContractError("at least one symbol is required")
        if any(not symbol or symbol != symbol.upper() for symbol in self.symbols):
            raise CaptureContractError("symbols must be non-empty uppercase identifiers")
        try:
            parsed_market_date = date.fromisoformat(self.market_date)
        except ValueError as exc:
            raise CaptureContractError("market_date must be an ISO date") from exc
        if parsed_market_date.isoformat() != self.market_date:
            raise CaptureContractError("market_date must be an ISO date")
        if not self.exchange_session_id.strip():
            raise CaptureContractError("exchange_session_id is required")
        _require_utc(self.request_start, "request_start")
        _require_utc(self.request_end, "request_end")
        if self.request_end <= self.request_start:
            raise CaptureContractError("request_end must be after request_start")
        if not _GIT_OID.fullmatch(self.code_sha):
            raise CaptureContractError("code_sha must be an exact lowercase Git object id")
        if not _SHA256.fullmatch(self.source_config_hash):
            raise CaptureContractError("source_config_hash must be a lowercase SHA-256")
        if not isinstance(self.operator_entitlement_metadata, dict):
            raise CaptureContractError("operator entitlement metadata must be an object")
        entitlement = str(self.operator_entitlement_metadata.get("entitlement") or "").strip()
        proof_id = str(
            self.operator_entitlement_metadata.get("receipt")
            or self.operator_entitlement_metadata.get("proof_id")
            or ""
        ).strip()
        if not entitlement or not proof_id:
            raise CaptureContractError(
                "operator entitlement metadata requires entitlement and receipt/proof_id"
            )


@dataclass
class _EndpointState:
    pages: list[dict[str, Any]] = field(default_factory=list)
    next_page_token: str | None = None
    complete: bool = False
    status: str = "PENDING"
    reason: str = ""
    artifact_manifest_id: str | None = None
    aggregate_raw_hash: str = ""
    aggregate_normalized_hash: str = ""


class IntradayEvidenceCaptureService:
    """Capture bars (required) and optional source pages with safe resumption."""

    def __init__(
        self,
        provider: HistoricalIntradayProvider,
        config: ScannerConfig,
        *,
        store: IntradayEvidenceStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.config = config
        self.store = store
        self._sleep = sleep

    def capture(self, request: CaptureRequest) -> dict[str, Any]:
        request.validate()
        self._validate_provider(request)
        store = self.store or IntradayEvidenceStore(
            request.db_path, evidence_root=request.evidence_root, code_sha=request.code_sha
        )
        store.initialize()

        run_id = _run_id(request)
        run_dir = request.run_root.resolve() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "capture_run_state.json"
        receipt_path = run_dir / "capture_run_receipt.json"
        state = _load_state(state_path, request, run_id)
        state["status"] = "RUNNING"
        state["updated_at"] = _now().isoformat()
        _atomic_json_write(state_path, state)

        endpoint_names = ["bars"]
        if request.include_trades:
            endpoint_names.append("trades")
        if request.include_quotes:
            endpoint_names.append("quotes")
        if request.include_corporate_actions:
            endpoint_names.append("corporate_actions")

        for symbol in request.symbols:
            symbol_state = state["symbols"].setdefault(symbol, {})
            for endpoint in endpoint_names:
                endpoint_state = _state_from_dict(symbol_state.setdefault(endpoint, {}))
                if endpoint_state.complete:
                    continue
                try:
                    self._capture_endpoint(
                        request=request,
                        run_id=run_id,
                        run_dir=run_dir,
                        symbol=symbol,
                        endpoint=endpoint,
                        state=endpoint_state,
                        store=store,
                        checkpoint=partial(
                            _persist_endpoint_checkpoint,
                            state_path,
                            state,
                            symbol_state,
                            endpoint,
                            endpoint_state,
                        ),
                    )
                except SourceConflictError as exc:
                    endpoint_state.status = IntradayCoverageStatus.SOURCE_CONFLICT.value
                    endpoint_state.reason = str(exc)
                    endpoint_state.complete = True
                except CaptureContractError as exc:
                    endpoint_state.status = IntradayCoverageStatus.HASH_MISMATCH.value
                    endpoint_state.reason = str(exc)
                    endpoint_state.complete = True
                except Exception as exc:  # provider failures are receipt facts, not crashes
                    endpoint_state.status = _provider_failure_status(exc)
                    endpoint_state.reason = _safe_error(exc)
                    endpoint_state.complete = True
                symbol_state[endpoint] = _endpoint_to_dict(endpoint_state)
                state["updated_at"] = _now().isoformat()
                _atomic_json_write(state_path, state)

        coverage: list[dict[str, Any]] = []
        for symbol in request.symbols:
            symbol_state = state["symbols"][symbol]
            bars = _state_from_dict(symbol_state.get("bars", {}))
            optional_statuses = [
                _state_from_dict(symbol_state[name]).status
                for name in endpoint_names
                if name != "bars" and name in symbol_state
            ]
            status = bars.status or IntradayCoverageStatus.NO_DATA.value
            reason = bars.reason
            if status == IntradayCoverageStatus.COMPLETE.value and any(
                value == IntradayCoverageStatus.CORPORATE_ACTION_UNRESOLVED.value
                for value in optional_statuses
            ):
                status = IntradayCoverageStatus.CORPORATE_ACTION_UNRESOLVED.value
                reason = "corporate-action source was requested but unresolved"
            coverage.append(
                self._record_coverage(
                    request=request,
                    symbol=symbol,
                    status=status,
                    reason=reason,
                    endpoint_state=bars,
                    store=store,
                )
            )

        overall = _overall_status(coverage)
        state["status"] = overall
        state["coverage"] = coverage
        state["updated_at"] = _now().isoformat()
        _atomic_json_write(state_path, state)
        receipt = {
            "schema_version": "dawnstrike.intraday_capture_run.v1",
            "run_id": run_id,
            "status": overall,
            "provider": request.provider,
            "feed": request.feed,
            "evidence_mode": request.evidence_mode,
            "symbols": list(request.symbols),
            "market_date": request.market_date,
            "exchange_session_id": request.exchange_session_id,
            "request_start": request.request_start.isoformat(),
            "request_end": request.request_end.isoformat(),
            "code_sha": request.code_sha,
            "source_config_hash": request.source_config_hash,
            "operator_entitlement_metadata": request.operator_entitlement_metadata,
            "coverage": coverage,
            "state_path": str(state_path),
            "created_at": _now().isoformat(),
        }
        receipt["receipt_hash_sha256"] = _sha256(_canonical_json(receipt))
        _atomic_json_write(receipt_path, receipt)
        return receipt

    def _capture_endpoint(
        self,
        *,
        request: CaptureRequest,
        run_id: str,
        run_dir: Path,
        symbol: str,
        endpoint: str,
        state: _EndpointState,
        store: IntradayEvidenceStore,
        checkpoint: Callable[[], None],
    ) -> None:
        fetch = getattr(self.provider, _ENDPOINTS[endpoint])
        _validate_checkpoint_pages(
            state.pages,
            provider=request.provider,
            feed=request.feed,
            endpoint=endpoint,
        )
        page_token = state.next_page_token
        if state.pages and page_token != state.pages[-1].get("cursor_out"):
            raise CaptureContractError("capture checkpoint next cursor changed")
        if not state.pages and page_token is not None:
            raise CaptureContractError("capture checkpoint has a cursor without pages")
        previous_page_hash = None
        if state.pages:
            previous_page_hash = state.pages[-1].get("raw_payload_hash_sha256")
        for page_number in range(len(state.pages), self.config.historical_intraday_max_pages):
            page = self._fetch_page(
                fetch,
                symbol=symbol,
                request=request,
                page_token=page_token,
                endpoint=endpoint,
            )
            if page.provider != request.provider or page.feed != request.feed:
                raise CaptureContractError("provider/feed identity changed within capture")
            page_hash = str(page.raw_payload_hash_sha256 or "")
            if not _SHA256.fullmatch(page_hash):
                raise CaptureContractError("provider page has no valid raw payload hash")
            cursor_out = page.next_page_token
            if cursor_out is not None and not str(cursor_out).strip():
                raise CaptureContractError("provider returned a blank pagination cursor")
            if cursor_out is not None and cursor_out == page_token:
                raise CaptureContractError("provider pagination cursor did not advance")
            page_record = {
                "page_number": page_number,
                "cursor_in": page_token,
                "cursor_out": cursor_out,
                "provider": page.provider,
                "feed": page.feed,
                "endpoint": page.endpoint,
                "request_id": page.request_id,
                "raw_payload_hash_sha256": page_hash,
                "item_count": len(page.items),
                "previous_page_hash_sha256": previous_page_hash,
            }
            page_dir = run_dir / "pages" / symbol / endpoint
            page_path = page_dir / f"page-{page_number:06d}.json"
            page_envelope = {**page_record, "items": list(page.items)}
            _atomic_json_write(page_path, page_envelope)
            raw_bytes = _canonical_json(page_envelope)
            normalized_bytes = _canonical_json(
                {"schema_version": "dawnstrike.intraday_page.v1", "items": list(page.items)}
            )
            manifest = store.store_artifact(
                provider=request.provider,
                feed=request.feed,
                artifact_kind=f"intraday-{endpoint}-page-{page_number:06d}",
                symbol=symbol,
                market_date=request.market_date,
                exchange_session_id=request.exchange_session_id,
                entitlement=_entitlement_name(request.operator_entitlement_metadata),
                request_start=request.request_start,
                request_end=request.request_end,
                fetched_at=_now(),
                raw_bytes=raw_bytes,
                normalized_bytes=normalized_bytes,
                retention_allowed=True,
                retention_status="retained",
                code_sha=request.code_sha,
                metadata={
                    "run_id": run_id,
                    "evidence_mode": request.evidence_mode,
                    "source_config_hash": request.source_config_hash,
                    "operator_entitlement_metadata": request.operator_entitlement_metadata,
                    "page_number": page_number,
                    "provider_raw_payload_hash_sha256": page_hash,
                },
            )
            page_record["page_path"] = str(page_path)
            page_record["artifact_manifest_id"] = manifest.artifact_manifest_id
            page_record["raw_artifact_hash_sha256"] = manifest.raw_artifact_hash_sha256
            page_record["normalized_artifact_hash_sha256"] = (
                manifest.normalized_artifact_hash_sha256
            )
            state.pages.append(page_record)
            previous_page_hash = page_hash
            page_token = cursor_out
            state.next_page_token = page_token
            checkpoint()
            if page_token is None:
                state.complete = True
                state.status = (
                    IntradayCoverageStatus.COMPLETE.value
                    if page_number >= 0
                    and any(page_record["item_count"] for page_record in state.pages)
                    else IntradayCoverageStatus.NO_DATA.value
                )
                break
        else:
            state.status = IntradayCoverageStatus.PARTIAL_MISSING_INTERVALS.value
            state.reason = "historical pagination limit reached before the cursor chain completed"
            return

        if state.complete and state.pages:
            aggregate = {
                "schema_version": "dawnstrike.intraday_capture_aggregate.v1",
                "provider": request.provider,
                "feed": request.feed,
                "endpoint": endpoint,
                "symbol": symbol,
                "market_date": request.market_date,
                "exchange_session_id": request.exchange_session_id,
                "pages": [
                    {
                        "page_number": page["page_number"],
                        "raw_payload_hash_sha256": page["raw_payload_hash_sha256"],
                        "artifact_manifest_id": page["artifact_manifest_id"],
                        "item_count": page["item_count"],
                    }
                    for page in state.pages
                ],
                "items": _read_page_items(state.pages),
            }
            aggregate_bytes = _canonical_json(aggregate)
            aggregate_manifest = store.store_artifact(
                provider=request.provider,
                feed=request.feed,
                artifact_kind=f"intraday-{endpoint}-aggregate",
                symbol=symbol,
                market_date=request.market_date,
                exchange_session_id=request.exchange_session_id,
                entitlement=_entitlement_name(request.operator_entitlement_metadata),
                request_start=request.request_start,
                request_end=request.request_end,
                fetched_at=_now(),
                raw_bytes=aggregate_bytes,
                normalized_bytes=aggregate_bytes,
                retention_allowed=True,
                retention_status="retained",
                code_sha=request.code_sha,
                metadata={
                    "run_id": run_id,
                    "evidence_mode": request.evidence_mode,
                    "source_config_hash": request.source_config_hash,
                    "operator_entitlement_metadata": request.operator_entitlement_metadata,
                    "page_hash_chain": [page["raw_payload_hash_sha256"] for page in state.pages],
                },
            )
            state.artifact_manifest_id = aggregate_manifest.artifact_manifest_id
            state.aggregate_raw_hash = aggregate_manifest.raw_artifact_hash_sha256
            state.aggregate_normalized_hash = aggregate_manifest.normalized_artifact_hash_sha256

    def _record_coverage(
        self,
        *,
        request: CaptureRequest,
        symbol: str,
        status: str,
        reason: str,
        endpoint_state: _EndpointState,
        store: IntradayEvidenceStore,
    ) -> dict[str, Any]:
        pages = endpoint_state.pages
        items = _read_page_items(pages)
        observed: list[datetime] = []
        for item in items:
            timestamp = _timestamp(item)
            if timestamp is not None:
                observed.append(timestamp)
        aggregate_manifest_id = endpoint_state.artifact_manifest_id
        raw_hash = ""
        normalized_hash = ""
        if aggregate_manifest_id:
            raw_hash = endpoint_state.aggregate_raw_hash
            normalized_hash = endpoint_state.aggregate_normalized_hash
        if not raw_hash:
            raw_hash = str(pages[-1].get("raw_artifact_hash_sha256") or "") if pages else ""
            normalized_hash = (
                str(pages[-1].get("normalized_artifact_hash_sha256") or "") if pages else ""
            )
        now = _now()
        source = IntradaySourceMetadata(
            provider=request.provider,
            feed=request.feed,
            entitlement=_entitlement_name(request.operator_entitlement_metadata),
            exchange_session_id=request.exchange_session_id,
            request_start=request.request_start,
            request_end=request.request_end,
            fetched_at=now,
            code_sha=request.code_sha,
            raw_artifact_hash_sha256=raw_hash,
            normalized_artifact_hash_sha256=normalized_hash,
            retention_status="retained" if pages else "not_available",
        )
        receipt = IntradayCoverageReceipt(
            coverage_receipt_id=_sha256(
                _canonical_json(
                    {
                        "provider": request.provider,
                        "feed": request.feed,
                        "symbol": symbol,
                        "market_date": request.market_date,
                        "exchange_session_id": request.exchange_session_id,
                        "request_start": request.request_start.isoformat(),
                        "request_end": request.request_end.isoformat(),
                    }
                )
            ),
            provider=request.provider,
            feed=request.feed,
            entitlement=_entitlement_name(request.operator_entitlement_metadata),
            symbol=symbol,
            market_date=request.market_date,
            exchange_session_id=request.exchange_session_id,
            request_start=request.request_start,
            request_end=request.request_end,
            status=IntradayCoverageStatus(status),
            source_metadata=source,
            observed_start=min(observed) if observed else None,
            observed_end=max(observed) if observed else None,
            artifact_manifest_ids=tuple(
                page["artifact_manifest_id"] for page in pages if page.get("artifact_manifest_id")
            ),
            reason=reason,
            created_at=now,
        )
        # A bounded pagination run may be resumed.  Do not publish a partial
        # database receipt that would make the immutable identity conflict
        # with the eventual aggregate; the run-file receipt still records the
        # partial truth and its cursor checkpoint.
        if not (
            status == IntradayCoverageStatus.PARTIAL_MISSING_INTERVALS.value
            and endpoint_state.next_page_token is not None
        ):
            store.record_coverage(receipt)
        return {
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "coverage_receipt_id": receipt.coverage_receipt_id,
            "artifact_manifest_ids": list(receipt.artifact_manifest_ids),
            "observed_start": (
                receipt.observed_start.isoformat() if receipt.observed_start else None
            ),
            "observed_end": receipt.observed_end.isoformat() if receipt.observed_end else None,
            "source_metadata": source.to_dict(),
        }

    def _fetch_page(
        self,
        fetch: Callable[..., IntradayPage],
        *,
        symbol: str,
        request: CaptureRequest,
        page_token: str | None,
        endpoint: str,
    ) -> IntradayPage:
        last_error: Exception | None = None
        for attempt in range(1, self.config.request_retries + 1):
            try:
                return fetch(
                    [symbol],
                    request.request_start.isoformat(),
                    request.request_end.isoformat(),
                    self.config,
                    page_token=page_token,
                )
            except Exception as exc:
                last_error = exc
                if _provider_failure_status(exc) == IntradayCoverageStatus.ENTITLEMENT_DENIED.value:
                    raise
                if attempt < self.config.request_retries:
                    delay = min(
                        self.config.historical_intraday_backoff_seconds
                        * (2 ** (attempt - 1)),
                        8.0,
                    )
                    self._sleep(delay)
        raise CaptureContractError(
            f"{endpoint} page failed after bounded retries: {_safe_error(last_error)}"
        ) from last_error

    def _validate_provider(self, request: CaptureRequest) -> None:
        name = str(getattr(self.provider, "provider_name", "")).strip()
        feed = str(getattr(self.provider, "feed", "")).strip()
        if name != request.provider or feed != request.feed:
            raise CaptureContractError(
                f"provider identity mismatch: requested {request.provider}/{request.feed}, "
                f"provider is {name}/{feed}"
            )


def _load_state(path: Path, request: CaptureRequest, run_id: str) -> dict[str, Any]:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("run_id") != run_id or payload.get("request") != _request_json(request):
                raise CaptureContractError(
                    "existing capture checkpoint identity conflicts with request"
                )
            payload.setdefault("symbols", {})
            return payload
        except (OSError, json.JSONDecodeError) as exc:
            raise CaptureContractError("capture checkpoint is unreadable") from exc
    return {
        "schema_version": "dawnstrike.intraday_capture_state.v1",
        "run_id": run_id,
        "request": _request_json(request),
        "status": "PENDING",
        "symbols": {symbol: {} for symbol in request.symbols},
        "updated_at": _now().isoformat(),
    }


def _request_json(request: CaptureRequest) -> dict[str, Any]:
    value = asdict(request)
    for key in ("db_path", "evidence_root", "run_root"):
        value[key] = str(value[key])
    value["request_start"] = request.request_start.isoformat()
    value["request_end"] = request.request_end.isoformat()
    value["symbols"] = list(request.symbols)
    return value


def _state_from_dict(value: dict[str, Any]) -> _EndpointState:
    return _EndpointState(
        pages=list(value.get("pages") or []),
        next_page_token=value.get("next_page_token"),
        complete=bool(value.get("complete", False)),
        status=str(value.get("status") or "PENDING"),
        reason=str(value.get("reason") or ""),
        artifact_manifest_id=value.get("artifact_manifest_id"),
        aggregate_raw_hash=str(value.get("aggregate_raw_hash") or ""),
        aggregate_normalized_hash=str(value.get("aggregate_normalized_hash") or ""),
    )


def _endpoint_to_dict(state: _EndpointState) -> dict[str, Any]:
    return {
        "pages": state.pages,
        "next_page_token": state.next_page_token,
        "complete": state.complete,
        "status": state.status,
        "reason": state.reason,
        "artifact_manifest_id": state.artifact_manifest_id,
        "aggregate_raw_hash": state.aggregate_raw_hash,
        "aggregate_normalized_hash": state.aggregate_normalized_hash,
    }


def _persist_endpoint_checkpoint(
    state_path: Path,
    state: dict[str, Any],
    symbol_state: dict[str, Any],
    endpoint: str,
    endpoint_state: _EndpointState,
) -> None:
    symbol_state[endpoint] = _endpoint_to_dict(endpoint_state)
    state["updated_at"] = _now().isoformat()
    _atomic_json_write(state_path, state)


def _read_page_items(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in pages:
        path = page.get("page_path")
        if not path:
            continue
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CaptureContractError(f"page artifact is unreadable: {path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise CaptureContractError(f"page artifact has invalid normalized items: {path}")
        result.extend(item for item in payload["items"] if isinstance(item, dict))
    return result


def _validate_checkpoint_pages(
    pages: list[dict[str, Any]],
    *,
    provider: str,
    feed: str,
    endpoint: str,
) -> None:
    """Reject a resumed cursor chain whose durable page evidence changed."""

    previous_hash: str | None = None
    previous_cursor: str | None = None
    for expected_number, page in enumerate(pages):
        if page.get("page_number") != expected_number:
            raise CaptureContractError("capture checkpoint page sequence is not contiguous")
        if (
            page.get("provider") != provider
            or page.get("feed") != feed
            or page.get("endpoint") != endpoint
        ):
            raise CaptureContractError("capture checkpoint provider/feed identity changed")
        if page.get("cursor_in") != previous_cursor:
            raise CaptureContractError("capture checkpoint cursor chain changed")
        page_hash = str(page.get("raw_payload_hash_sha256") or "")
        if not _SHA256.fullmatch(page_hash):
            raise CaptureContractError("capture checkpoint page hash is invalid")
        if page.get("previous_page_hash_sha256") != previous_hash:
            raise CaptureContractError("capture checkpoint page hash chain changed")
        path_value = str(page.get("page_path") or "")
        if not path_value:
            raise CaptureContractError("capture checkpoint page artifact is missing")
        try:
            envelope = json.loads(Path(path_value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CaptureContractError("capture checkpoint page artifact is unreadable") from exc
        if not isinstance(envelope, dict):
            raise CaptureContractError("capture checkpoint page artifact is invalid")
        stored_artifact_hash = str(page.get("raw_artifact_hash_sha256") or "")
        if _sha256(_canonical_json(envelope)) != stored_artifact_hash:
            raise CaptureContractError("capture checkpoint page artifact hash changed")
        previous_hash = page_hash
        previous_cursor = page.get("cursor_out")


def _timestamp(item: dict[str, Any]) -> datetime | None:
    raw = (
        item.get("t")
        or item.get("timestamp")
        or item.get("sip_timestamp")
        or item.get("participant_timestamp")
    )
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(UTC) if value.tzinfo else None


def _provider_failure_status(error: Exception | None) -> str:
    message = _safe_error(error).lower()
    if any(
        token in message
        for token in ("401", "403", "entitlement", "credential", "permission denied")
    ):
        return IntradayCoverageStatus.ENTITLEMENT_DENIED.value
    if "corporate" in message and "action" in message:
        return IntradayCoverageStatus.CORPORATE_ACTION_UNRESOLVED.value
    if "hash" in message or "cursor" in message:
        return IntradayCoverageStatus.HASH_MISMATCH.value
    return IntradayCoverageStatus.PARTIAL_MISSING_INTERVALS.value


def _overall_status(coverage: list[dict[str, Any]]) -> str:
    statuses = {str(row.get("status")) for row in coverage}
    if statuses == {IntradayCoverageStatus.COMPLETE.value}:
        return IntradayCoverageStatus.COMPLETE.value
    if len(statuses) == 1:
        return next(iter(statuses))
    if statuses and statuses <= {
        IntradayCoverageStatus.NO_DATA.value,
        IntradayCoverageStatus.COMPLETE.value,
    }:
        return (
            IntradayCoverageStatus.NO_DATA.value
            if statuses == {IntradayCoverageStatus.NO_DATA.value}
            else "PARTIAL"
        )
    return "PARTIAL"


def _run_id(request: CaptureRequest) -> str:
    return _sha256(_canonical_json(_request_json(request)))[:32]


def _entitlement_name(metadata: dict[str, Any]) -> str:
    value = metadata.get("entitlement") or metadata.get("entitlement_id") or metadata.get("plan")
    return str(value).strip() if value is not None and str(value).strip() else "operator_declared"


def _safe_error(error: Exception | None) -> str:
    if error is None:
        return ""
    return str(error).replace("\r", " ").replace("\n", " ")[:500]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise CaptureContractError(f"{field_name} must be timezone-aware UTC")


__all__ = [
    "CaptureContractError",
    "CaptureRequest",
    "IntradayEvidenceCaptureService",
]
