"""Shared strict public-build lineage primitives.

The V6 learning projection is an immutable byte input to the public build
identity.  Keep the validation deliberately small so it can also be mirrored
by the PowerShell publisher and the Vercel readiness function.
"""

from __future__ import annotations

import hashlib
import re

LOWER_HEX64 = re.compile(r"^[0-9a-f]{64}$")
ISO_MARKET_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_lower_hex64(value: object) -> bool:
    return isinstance(value, str) and LOWER_HEX64.fullmatch(value) is not None


def build_sha(
    *,
    source_sha: str,
    publication_set_sha256: str,
    opportunity_projection_sha256: str,
    v6_learning_sha256: str,
    market_date: str,
) -> str:
    """Compute the documented five-input public build identity."""

    formula = (
        f"{source_sha}:{publication_set_sha256}:{opportunity_projection_sha256}:"
        f"{v6_learning_sha256}:{market_date}"
    )
    return hashlib.sha256(formula.encode("utf-8")).hexdigest()
