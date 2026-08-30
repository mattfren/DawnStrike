"""Trusted FillTruth boundary for learning and publication consumers.

Canonical bar replay and caller-provided hashes are useful diagnostics, but
neither proves that a paper position was filled and closed at the relevant
point in time.  The current checkout has no governed CommitBridge resolver
that can authenticate such a join.  Keep this boundary fail closed until one
is supplied by the private execution/evidence adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from intraday_scanner.alpha.commit_bridge import (
    AuthenticatedFillTruth,
    has_authenticated_fill_truth,
)

MISSING_COMMITTED_FILL_TRUTH = "committed_point_in_time_fill_truth_required"


def has_authenticated_committed_fill_truth(value: Mapping[str, Any] | object) -> bool:
    """Return whether *value* was authenticated by the governed FillTruth join.

    No JSON/durable mapping is authoritative by itself.  In particular,
    ``fill_truth_status``, a digest, or a self-hashed receipt is caller data,
    not authentication.  Until a private CommitBridge source is wired into
    this boundary, all mappings remain provisional and ineligible.
    """

    return has_authenticated_fill_truth(value)


__all__ = [
    "MISSING_COMMITTED_FILL_TRUTH",
    "AuthenticatedFillTruth",
    "has_authenticated_committed_fill_truth",
]
