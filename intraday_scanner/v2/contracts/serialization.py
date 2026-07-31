"""Deterministic serialization for v2 contracts."""

from __future__ import annotations

import json
import types
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints


class ContractValidationError(ValueError):
    """Raised when a contract payload violates the typed contract."""


ContractT = TypeVar("ContractT", bound="ContractMixin")


class ContractMixin:
    """Shared helpers for frozen dataclass contracts."""

    def __post_init__(self) -> None:
        validate_contract_instance(self)

    def to_dict(self) -> dict[str, Any]:
        return contract_to_dict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return contract_to_json(self, indent=indent)

    @classmethod
    def from_dict(cls: type[ContractT], payload: dict[str, Any]) -> ContractT:
        return contract_from_dict(cls, payload)

    @classmethod
    def from_json(cls: type[ContractT], payload: str) -> ContractT:
        return contract_from_json(cls, payload)


def contract_to_dict(value: Any) -> dict[str, Any]:
    built = _to_builtin(value)
    if not isinstance(built, dict):
        raise TypeError("contract root must serialize to a JSON object")
    return built


def contract_to_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        _to_builtin(value),
        ensure_ascii=True,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
        sort_keys=True,
    )


def contract_from_json(cls: type[ContractT], payload: str) -> ContractT:
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ContractValidationError("contract JSON root must be an object")
    return contract_from_dict(cls, decoded)


def contract_from_dict(cls: type[ContractT], payload: dict[str, Any]) -> ContractT:
    if not is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass contract")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    missing: list[str] = []
    for field in fields(cls):
        if field.name in payload:
            kwargs[field.name] = _decode_value(payload[field.name], hints.get(field.name, Any))
        elif field.default is MISSING and field.default_factory is MISSING:
            missing.append(field.name)
    if missing:
        joined = ", ".join(missing)
        raise ContractValidationError(f"missing required contract field(s): {joined}")
    return cls(**kwargs)


def validate_contract_instance(instance: Any) -> None:
    if not is_dataclass(instance):
        raise TypeError("contract validation requires a dataclass instance")
    hints = get_type_hints(type(instance))
    for field in fields(instance):
        expected = hints.get(field.name, Any)
        _validate_value(getattr(instance, field.name), expected, field.name)


def _to_builtin(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_builtin(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        _ensure_aware_datetime(value, "datetime")
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple | list):
        return [_to_builtin(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_builtin(value[key]) for key in sorted(value, key=str)}
    return value


def _decode_value(value: Any, expected: Any) -> Any:
    origin = get_origin(expected)
    args = get_args(expected)

    if expected is Any or expected is object:
        return value
    if expected is type(None):
        if value is None:
            return None
        raise ContractValidationError("expected null")
    if _is_union_origin(origin):
        errors: list[Exception] = []
        for option in args:
            try:
                return _decode_value(value, option)
            except (TypeError, ValueError, ContractValidationError) as exc:
                errors.append(exc)
        raise ContractValidationError(f"value did not match any allowed type: {errors[0]}")
    if origin is tuple:
        if not isinstance(value, list | tuple):
            raise ContractValidationError("expected array for tuple field")
        item_type = args[0] if args and args[-1] is Ellipsis else Any
        return tuple(_decode_value(item, item_type) for item in value)
    if origin is list:
        if not isinstance(value, list):
            raise ContractValidationError("expected array for list field")
        item_type = args[0] if args else Any
        return [_decode_value(item, item_type) for item in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ContractValidationError("expected object for dict field")
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        return {
            _decode_value(key, key_type): _decode_value(item, value_type)
            for key, item in value.items()
        }
    if isinstance(expected, type) and issubclass(expected, Enum):
        if isinstance(value, expected):
            return value
        return expected(value)
    if expected is datetime:
        if isinstance(value, datetime):
            return _ensure_aware_datetime(value, "datetime")
        if isinstance(value, str):
            normalized = value.replace("Z", "+00:00")
            return _ensure_aware_datetime(datetime.fromisoformat(normalized), "datetime")
        raise ContractValidationError("expected datetime string")
    if expected is Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str | int | float):
            return Decimal(str(value))
        raise ContractValidationError("expected decimal-compatible value")
    if isinstance(expected, type) and is_dataclass(expected):
        if isinstance(value, expected):
            return value
        if isinstance(value, dict):
            return contract_from_dict(cast(type[ContractMixin], expected), value)
        raise ContractValidationError(f"expected object for {expected.__name__}")
    _validate_value(value, expected, "value")
    return value


def _validate_value(value: Any, expected: Any, field_name: str) -> None:
    origin = get_origin(expected)
    args = get_args(expected)

    if expected is Any or expected is object:
        return
    if expected is type(None):
        if value is not None:
            raise TypeError(f"{field_name} must be None")
        return
    if _is_union_origin(origin):
        for option in args:
            try:
                _validate_value(value, option, field_name)
                return
            except (TypeError, ContractValidationError):
                continue
        raise TypeError(f"{field_name} does not match any allowed type")
    if origin is tuple:
        if not isinstance(value, tuple):
            raise TypeError(f"{field_name} must be a tuple")
        item_type = args[0] if args and args[-1] is Ellipsis else Any
        for item in value:
            _validate_value(item, item_type, field_name)
        return
    if origin is list:
        if not isinstance(value, list):
            raise TypeError(f"{field_name} must be a list")
        item_type = args[0] if args else Any
        for item in value:
            _validate_value(item, item_type, field_name)
        return
    if origin is dict:
        if not isinstance(value, dict):
            raise TypeError(f"{field_name} must be a dict")
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        for key, item in value.items():
            _validate_value(key, key_type, field_name)
            _validate_value(item, value_type, field_name)
        return
    if isinstance(expected, type) and issubclass(expected, Enum):
        if not isinstance(value, expected):
            raise TypeError(f"{field_name} must be {expected.__name__}")
        return
    if expected is datetime:
        if not isinstance(value, datetime):
            raise TypeError(f"{field_name} must be datetime")
        _ensure_aware_datetime(value, field_name)
        return
    if expected is Decimal:
        if not isinstance(value, Decimal):
            raise TypeError(f"{field_name} must be Decimal")
        return
    if isinstance(expected, type) and is_dataclass(expected):
        if not isinstance(value, expected):
            raise TypeError(f"{field_name} must be {expected.__name__}")
        validate_contract_instance(value)
        return
    if expected is str:
        if type(value) is not str:
            raise TypeError(f"{field_name} must be str")
        return
    if expected is int:
        if type(value) is not int:
            raise TypeError(f"{field_name} must be int")
        return
    if expected is float:
        if type(value) is not float:
            raise TypeError(f"{field_name} must be float")
        return
    if expected is bool:
        if type(value) is not bool:
            raise TypeError(f"{field_name} must be bool")
        return
    if isinstance(expected, type) and not isinstance(value, expected):
        raise TypeError(f"{field_name} must be {expected.__name__}")


def _ensure_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractValidationError(f"{field_name} must be timezone-aware")
    return value


def _is_union_origin(origin: Any) -> bool:
    return origin is Union or origin is types.UnionType
