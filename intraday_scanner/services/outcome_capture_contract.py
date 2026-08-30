"""Shared truth-state classification for outcome-capture consumers."""

from __future__ import annotations

from typing import Any


def classify_missing_capture(row: dict[str, Any]) -> str:
    """Classify a missing capture without turning absence into an outcome.

    New writers persist an explicit classification.  Legacy terminal rows are
    conservatively authoritative-terminal; unknown rows are resolved here and
    remain in the caller's unresolved/missing path rather than becoming a
    successful or zero-valued outcome.
    """

    explicit = str(
        row.get("missing_classification") or row.get("missing_state") or ""
    ).strip().lower()
    if explicit in {"recoverable", "authoritative_terminal", "resolved"}:
        return explicit
    if str(row.get("status") or "").strip().lower() == "terminal_missing":
        return "authoritative_terminal"
    if str(row.get("outcome_status") or "").strip().upper() == "TERMINAL_MISSING":
        return "authoritative_terminal"
    return "resolved"


__all__ = ["classify_missing_capture"]
