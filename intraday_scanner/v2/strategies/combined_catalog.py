"""Additive official strategy catalog with stable legacy identities."""

from __future__ import annotations

from intraday_scanner.v2.strategies.catalog import (
    build_strategy_catalog as build_legacy_strategy_catalog,
)
from intraday_scanner.v2.strategies.models import StrategySpec
from intraday_scanner.v2.strategies.research import build_research_strategy_catalog

_COMPARATOR_STATUSES = frozenset({"baseline", "benchmark"})


def build_strategy_catalog() -> tuple[StrategySpec, ...]:
    """Return legacy alphas, additive research alphas, then comparators.

    Legacy signal implementations intentionally remain in their original module.
    Strategy identity hashes include the complete implementation-module source, so
    appending research implementations to that module would silently mutate every
    existing strategy fingerprint.
    """

    legacy = build_legacy_strategy_catalog()
    legacy_alphas = tuple(
        strategy for strategy in legacy if strategy.status not in _COMPARATOR_STATUSES
    )
    comparators = tuple(
        strategy for strategy in legacy if strategy.status in _COMPARATOR_STATUSES
    )
    catalog = (*legacy_alphas, *build_research_strategy_catalog(), *comparators)
    identities = [(strategy.strategy_id, strategy.version) for strategy in catalog]
    if len(identities) != len(set(identities)):
        raise ValueError("strategy catalog contains duplicate strategy identity/version pairs")
    return catalog
