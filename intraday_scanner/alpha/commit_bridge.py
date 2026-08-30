"""Private CommitBridge for persisted, point-in-time paper FillTruth.

The bridge is intentionally the only producer of an authenticated FillTruth
object.  A JSON mapping, a self-computed digest, or a caller-provided status
is diagnostic input and never crosses the learning boundary.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.errors import StorageError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_AUTHENTICATION_MARKER = object()
_IDENTITY_ALIASES = {
    "symbol": ("symbol", "ticker"),
    "run_id": ("run_id", "capture_run_id", "execution_run_id"),
    "session_id": ("session_id", "expected_session_id"),
    "fill_id": ("fill_id",),
    "order_id": ("order_id",),
    "position_id": ("position_id",),
    "intent_id": ("intent_id",),
    "decision_id": ("decision_id",),
    "selection_id": ("selection_id",),
    "account_id": ("account_id",),
    "strategy_id": ("strategy_id",),
    "strategy_version": ("strategy_version",),
    "market_date": ("market_date",),
}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _unfreeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _unfreeze(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_unfreeze(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FillTruthIdentity:
    """Exact identity dimensions used to resolve a persisted receipt."""

    account_id: str | None = None
    market_date: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    symbol: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    fill_id: str | None = None
    order_id: str | None = None
    position_id: str | None = None
    intent_id: str | None = None
    decision_id: str | None = None
    selection_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            key: str(value)
            for key, value in asdict(self).items()
            if value is not None and str(value).strip()
        }


@dataclass(frozen=True, slots=True)
class NoTradeIdentity:
    """Exact account/session identity for a finalized no-entry receipt."""

    account_id: str | None = None
    market_date: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    experiment_id: str | None = None
    arm_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            key: str(value)
            for key, value in asdict(self).items()
            if value is not None and str(value).strip()
        }


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedFillTruth(Mapping[str, Any]):
    """Immutable result minted only after CommitBridge verifies storage."""

    _payload: Mapping[str, Any]
    _authentication_marker: object

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._payload)

    def __len__(self) -> int:
        return len(self._payload)

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return _unfreeze(self._payload)

    def _is_intact(self) -> bool:
        if self._authentication_marker is not _AUTHENTICATION_MARKER:
            return False
        payload = self.to_dict()
        declared = str(payload.get("receipt_hash_sha256") or "")
        if not _SHA256.fullmatch(declared):
            return False
        unsigned = {key: value for key, value in payload.items() if key != "receipt_hash_sha256"}
        return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest() == declared


def has_authenticated_fill_truth(value: object) -> bool:
    """Return true only for an intact object minted by this bridge."""

    try:
        return isinstance(value, AuthenticatedFillTruth) and value._is_intact()
    except (AttributeError, TypeError, ValueError):
        return False


def _mint_authenticated_fill_truth(payload: Mapping[str, Any]) -> AuthenticatedFillTruth:
    """Private factory kept outside the public result type."""

    instance = object.__new__(AuthenticatedFillTruth)
    object.__setattr__(instance, "_payload", _freeze(dict(payload)))
    object.__setattr__(instance, "_authentication_marker", _AUTHENTICATION_MARKER)
    return instance


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedNoTradeReceipt(Mapping[str, Any]):
    """Immutable result minted only after persisted session receipt verification."""

    _payload: Mapping[str, Any]
    _authentication_marker: object

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._payload)

    def __len__(self) -> int:
        return len(self._payload)

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return _unfreeze(self._payload)

    def _is_intact(self) -> bool:
        if self._authentication_marker is not _AUTHENTICATION_MARKER:
            return False
        payload = self.to_dict()
        declared = str(payload.get("receipt_hash_sha256") or "")
        if not _SHA256.fullmatch(declared):
            return False
        unsigned = {key: value for key, value in payload.items() if key != "receipt_hash_sha256"}
        return (
            payload.get("status") == "FINALIZED"
            and payload.get("decision") == "NO_TRADE"
            and payload.get("no_entry") is True
            and payload.get("research_only") is True
            and payload.get("broker_execution_enabled") is False
            and hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest() == declared
        )


def has_authenticated_no_trade_receipt(value: object) -> bool:
    try:
        return isinstance(value, AuthenticatedNoTradeReceipt) and value._is_intact()
    except (AttributeError, TypeError, ValueError):
        return False


def _mint_authenticated_no_trade(payload: Mapping[str, Any]) -> AuthenticatedNoTradeReceipt:
    instance = object.__new__(AuthenticatedNoTradeReceipt)
    object.__setattr__(instance, "_payload", _freeze(dict(payload)))
    object.__setattr__(instance, "_authentication_marker", _AUTHENTICATION_MARKER)
    return instance


class CommitBridge:
    """Resolve committed FillTruth against immutable SQLite evidence."""

    def __init__(self, store: Any):
        if not hasattr(store, "load_committed_fill_truth_receipt_record"):
            raise TypeError("CommitBridge requires a committed FillTruth evidence store")
        self.store = store

    def resolve(
        self,
        receipt_id: str,
        *,
        identity: FillTruthIdentity | Mapping[str, Any] | None = None,
        expected_code_sha: str | None = None,
        expected_source_artifact_hash: str | None = None,
        expected_run_id: str | None = None,
    ) -> AuthenticatedFillTruth | None:
        """Resolve one exact receipt; every validation failure returns ``None``."""

        receipt_id = str(receipt_id or "").strip()
        if not receipt_id:
            return None
        try:
            record = self.store.load_committed_fill_truth_receipt_record(receipt_id)
        except (StorageError, OSError):
            return None
        if not isinstance(record, Mapping):
            return None
        columns = record.get("columns")
        payload = record.get("payload")
        payload_json = record.get("payload_json")
        if not isinstance(columns, Mapping) or not isinstance(payload, Mapping):
            return None
        payload = dict(payload)
        if canonical_json(payload) != str(payload_json or ""):
            return None
        if str(payload.get("receipt_id") or "") != receipt_id:
            return None
        declared_hash = str(payload.get("receipt_hash_sha256") or "").lower()
        if not _SHA256.fullmatch(declared_hash):
            return None
        unsigned = {key: value for key, value in payload.items() if key != "receipt_hash_sha256"}
        if hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest() != declared_hash:
            return None
        if not self._column_binding_is_exact(columns, payload):
            return None
        if not self._payload_is_committed(payload):
            return None
        if expected_code_sha is not None and str(payload.get("code_sha")) != str(expected_code_sha):
            return None
        if expected_source_artifact_hash is not None and str(
            payload.get("source_artifact_hash_sha256")
        ) != str(expected_source_artifact_hash):
            return None
        payload_run_id = self._value(payload, "run_id")
        if expected_run_id is not None and payload_run_id != str(expected_run_id):
            return None
        expected = (
            identity.to_dict() if isinstance(identity, FillTruthIdentity) else dict(identity or {})
        )
        if not self._identity_matches(payload, expected):
            return None
        return _mint_authenticated_fill_truth(payload)

    def resolve_required(self, receipt_id: str, **kwargs: Any) -> AuthenticatedFillTruth:
        result = self.resolve(receipt_id, **kwargs)
        if result is None:
            raise StorageError("committed FillTruth receipt could not be authenticated")
        return result

    @staticmethod
    def _value(payload: Mapping[str, Any], field: str) -> str | None:
        for alias in _IDENTITY_ALIASES.get(field, (field,)):
            value = payload.get(alias)
            if value is not None and str(value).strip():
                return str(value).strip().upper() if field == "symbol" else str(value).strip()
        return None

    @classmethod
    def _identity_matches(cls, payload: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        for field, aliases in _IDENTITY_ALIASES.items():
            supplied = next(
                (
                    expected.get(alias)
                    for alias in (field, *aliases)
                    if expected.get(alias) is not None
                ),
                None,
            )
            if supplied is None:
                continue
            actual = cls._value(payload, field)
            wanted = str(supplied).strip().upper() if field == "symbol" else str(supplied).strip()
            if not actual or actual != wanted:
                return False
        return True

    @staticmethod
    def _column_binding_is_exact(columns: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
        text_fields = (
            "receipt_id",
            "receipt_hash_sha256",
            "account_id",
            "strategy_id",
            "strategy_version",
            "experiment_id",
            "arm_id",
            "decision_id",
            "selection_id",
            "intent_id",
            "position_id",
            "order_id",
            "side",
            "market_date",
            "execution_status",
            "entry_at",
            "exit_at",
            "source_artifact_hash_sha256",
            "code_sha",
            "frozen_window",
            "created_at",
        )
        for field in text_fields:
            if _optional_text(columns.get(field)) != _optional_text(payload.get(field)):
                return False
        for field in (
            "quantity",
            "entry_price",
            "exit_price",
            "spread_cost_cents",
            "slippage_cost_cents",
            "fees_cents",
            "regulatory_cost_cents",
            "borrow_cost_cents",
        ):
            if not _numbers_equal(columns.get(field), payload.get(field)):
                return False
        return (
            columns.get("research_only") == 1
            and payload.get("research_only") is True
            and columns.get("broker_execution_enabled") == 0
            and payload.get("broker_execution_enabled") is False
        )

    @classmethod
    def _payload_is_committed(cls, payload: Mapping[str, Any]) -> bool:
        if (
            payload.get("research_only") is not True
            or payload.get("broker_execution_enabled") is not False
        ):
            return False
        if str(payload.get("execution_status") or "").upper() != "CLOSED":
            return False
        fill_status = payload.get("fill_truth_status")
        if fill_status is not None and str(fill_status).upper() != "COMMITTED":
            return False
        if payload.get("committed") is not True:
            return False
        if not str(payload.get("account_id") or "").strip():
            return False
        if not str(payload.get("strategy_id") or "").strip():
            return False
        if not str(payload.get("strategy_version") or "").strip():
            return False
        if not str(payload.get("market_date") or "").strip():
            return False
        if not _SHA256.fullmatch(str(payload.get("source_artifact_hash_sha256") or "")):
            return False
        if not _GIT_SHA.fullmatch(str(payload.get("code_sha") or "").lower()):
            return False
        if not str(payload.get("frozen_window") or "").strip():
            return False
        # A closed fill is only point-in-time truth when its join keys exist.
        return all(cls._value(payload, field) for field in ("symbol", "run_id", "fill_id"))


class NoTradeBridge:
    """Resolve only terminal, persisted no-entry session receipts."""

    def __init__(self, store: Any):
        if not hasattr(store, "load_no_trade_session_receipt_record"):
            raise TypeError("NoTradeBridge requires a no-trade evidence store")
        self.store = store

    def resolve(
        self,
        receipt_id: str,
        *,
        identity: NoTradeIdentity | Mapping[str, Any] | None = None,
        expected_code_sha: str | None = None,
        expected_source_artifact_hash: str | None = None,
        expected_source_config_hash: str | None = None,
        expected_calendar_source_hash: str | None = None,
        expected_run_id: str | None = None,
    ) -> AuthenticatedNoTradeReceipt | None:
        receipt_id = str(receipt_id or "").strip()
        if not receipt_id:
            return None
        try:
            record = self.store.load_no_trade_session_receipt_record(receipt_id)
        except (StorageError, OSError):
            return None
        if not isinstance(record, Mapping):
            return None
        columns, payload = record.get("columns"), record.get("payload")
        if not isinstance(columns, Mapping) or not isinstance(payload, Mapping):
            return None
        payload = dict(payload)
        if canonical_json(payload) != str(record.get("payload_json") or ""):
            return None
        if str(payload.get("receipt_id") or "") != receipt_id:
            return None
        declared = str(payload.get("receipt_hash_sha256") or "").lower()
        if not _SHA256.fullmatch(declared):
            return None
        unsigned = {key: value for key, value in payload.items() if key != "receipt_hash_sha256"}
        if hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest() != declared:
            return None
        if not self._columns_match(columns, payload):
            return None
        if not _SHA256.fullmatch(str(payload.get("source_artifact_hash_sha256") or "")):
            return None
        if not _SHA256.fullmatch(str(payload.get("source_config_hash_sha256") or "")):
            return None
        if not _SHA256.fullmatch(str(payload.get("calendar_source_hash_sha256") or "")):
            return None
        if not _GIT_SHA.fullmatch(str(payload.get("code_sha") or "").lower()):
            return None
        if not AuthenticatedNoTradeReceipt._is_intact(_mint_authenticated_no_trade(payload)):
            return None
        if expected_code_sha is not None and str(payload.get("code_sha")) != str(expected_code_sha):
            return None
        if expected_source_artifact_hash is not None and str(
            payload.get("source_artifact_hash_sha256")
        ) != str(expected_source_artifact_hash):
            return None
        if expected_source_config_hash is not None and str(
            payload.get("source_config_hash_sha256")
        ) != str(expected_source_config_hash):
            return None
        if expected_calendar_source_hash is not None and str(
            payload.get("calendar_source_hash_sha256")
        ) != str(expected_calendar_source_hash):
            return None
        if expected_run_id is not None and str(payload.get("run_id")) != str(expected_run_id):
            return None
        expected = (
            identity.to_dict() if isinstance(identity, NoTradeIdentity) else dict(identity or {})
        )
        for field in (
            "account_id",
            "market_date",
            "session_id",
            "run_id",
            "strategy_id",
            "strategy_version",
            "experiment_id",
            "arm_id",
        ):
            if field in expected and str(payload.get(field) or "") != str(expected[field]):
                return None
        return _mint_authenticated_no_trade(payload)

    def resolve_required(self, receipt_id: str, **kwargs: Any) -> AuthenticatedNoTradeReceipt:
        result = self.resolve(receipt_id, **kwargs)
        if result is None:
            raise StorageError("no-trade session receipt could not be authenticated")
        return result

    @staticmethod
    def _columns_match(columns: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
        fields = (
            "receipt_id",
            "receipt_hash_sha256",
            "account_id",
            "strategy_id",
            "strategy_version",
            "experiment_id",
            "arm_id",
            "market_date",
            "session_id",
            "run_id",
            "status",
            "decision",
            "source_artifact_hash_sha256",
            "source_config_hash_sha256",
            "calendar_source_hash_sha256",
            "code_sha",
            "created_at",
        )
        if any(
            _optional_text(columns.get(field)) != _optional_text(payload.get(field))
            for field in fields
        ):
            return False
        return (
            columns.get("no_entry") == 1
            and columns.get("research_only") == 1
            and columns.get("broker_execution_enabled") == 0
            and payload.get("no_entry") is True
            and payload.get("research_only") is True
            and payload.get("broker_execution_enabled") is False
        )


def resolve_committed_fill_truth(
    store: Any,
    receipt_id: str,
    **kwargs: Any,
) -> AuthenticatedFillTruth | None:
    """Convenience wrapper for consumers that do not need a bridge instance."""

    return CommitBridge(store).resolve(receipt_id, **kwargs)


def resolve_no_trade_receipt(
    store: Any,
    receipt_id: str,
    **kwargs: Any,
) -> AuthenticatedNoTradeReceipt | None:
    """Resolve one exact persisted no-entry session receipt."""

    return NoTradeBridge(store).resolve(receipt_id, **kwargs)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _numbers_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


__all__ = [
    "AuthenticatedFillTruth",
    "AuthenticatedNoTradeReceipt",
    "CommitBridge",
    "FillTruthIdentity",
    "NoTradeBridge",
    "NoTradeIdentity",
    "has_authenticated_fill_truth",
    "has_authenticated_no_trade_receipt",
    "resolve_committed_fill_truth",
    "resolve_no_trade_receipt",
]
