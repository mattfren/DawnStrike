"""Reusable Streamlit display helpers."""

from __future__ import annotations

from typing import Any


def filter_candidates(
    rows: list[dict[str, Any]], min_score: float, top_n: int
) -> list[dict[str, Any]]:
    filtered = [row for row in rows if float(row.get("score") or 0) >= min_score]
    return filtered[:top_n]
