"""Forward-unvalidated daily strategy research candidates."""

from intraday_scanner.v2.strategies.models import StrategySpec
from intraday_scanner.v2.strategies.research.gap_up_continuation import (
    build_strategy as build_gap_up_continuation,
)
from intraday_scanner.v2.strategies.research.gap_up_continuation_atr import (
    build_strategy as build_gap_up_continuation_atr,
)


def build_research_strategy_catalog() -> tuple[StrategySpec, ...]:
    """Return additive candidates admitted to forward paper observation."""

    return (build_gap_up_continuation(), build_gap_up_continuation_atr())


__all__ = ["build_research_strategy_catalog"]
