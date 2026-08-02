"""Small, explicit safety boundary for SQLite identifiers.

SQLite bind parameters protect values, but not table or column identifiers.
Any identifier that must be composed into a query passes through this module so
callers fail closed instead of relying on a convention that a string is safe.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIMPLE_ORDER = re.compile(
    r"^(?P<column>[A-Za-z_][A-Za-z0-9_]*)(?:\s+(?P<direction>ASC|DESC))?$",
    re.IGNORECASE,
)
_COALESCE_ORDER = re.compile(
    r"^COALESCE\(\s*(?P<column>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*"
    r"(?P<fallback>-?\d+)\s*\)\s+(?P<direction>ASC|DESC)$",
    re.IGNORECASE,
)


def quote_sql_identifier(identifier: str, *, allowed: Collection[str] | None = None) -> str:
    """Return one safely quoted SQLite identifier or raise before execution."""

    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe SQLite identifier: {identifier!r}")
    if allowed is not None and identifier not in allowed:
        raise ValueError(f"SQLite identifier is not allowlisted: {identifier}")
    return f'"{identifier}"'


def quote_sql_identifiers(
    identifiers: Iterable[str], *, allowed: Collection[str] | None = None
) -> str:
    """Quote a non-empty sequence of allowlisted SQLite identifiers."""

    quoted = [quote_sql_identifier(identifier, allowed=allowed) for identifier in identifiers]
    if not quoted:
        raise ValueError("at least one SQLite identifier is required")
    return ", ".join(quoted)


def quote_sql_order_by(order_by: str, *, allowed_columns: Collection[str]) -> str:
    """Validate a limited ORDER BY grammar and quote every referenced column."""

    if not order_by.strip():
        return ""
    components = _split_order_components(order_by)
    if not components:
        raise ValueError("ORDER BY must contain at least one component")
    rendered: list[str] = []
    for component in components:
        simple = _SIMPLE_ORDER.fullmatch(component)
        if simple is not None:
            column = quote_sql_identifier(simple.group("column"), allowed=allowed_columns)
            direction = (simple.group("direction") or "ASC").upper()
            rendered.append(f"{column} {direction}")
            continue
        coalesce = _COALESCE_ORDER.fullmatch(component)
        if coalesce is not None:
            column = quote_sql_identifier(coalesce.group("column"), allowed=allowed_columns)
            direction = coalesce.group("direction").upper()
            rendered.append(f"COALESCE({column}, {coalesce.group('fallback')}) {direction}")
            continue
        raise ValueError(f"unsafe SQLite ORDER BY component: {component!r}")
    return ", ".join(rendered)


def _split_order_components(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses in SQLite ORDER BY")
        elif character == "," and depth == 0:
            part = value[start:index].strip()
            if not part:
                raise ValueError("empty SQLite ORDER BY component")
            parts.append(part)
            start = index + 1
    if depth:
        raise ValueError("unbalanced parentheses in SQLite ORDER BY")
    final = value[start:].strip()
    if not final:
        raise ValueError("empty SQLite ORDER BY component")
    parts.append(final)
    return parts
