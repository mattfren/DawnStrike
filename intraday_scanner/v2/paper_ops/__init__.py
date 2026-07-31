"""PaperOps v1 forward/replay/demo paper-trading operations."""

from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.calendar_view import write_calendar_view
from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    calendar,
    check,
    close,
    enter,
    init,
    preflight,
    reconcile,
    replay,
    report,
    run_day,
    scan,
)
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.paper_ops.trade_blotter import (
    build_trade_blotter,
    verify_trade_blotter,
)

__all__ = [
    "PaperOpsPaths",
    "calendar",
    "check",
    "close",
    "enter",
    "init",
    "preflight",
    "reconcile",
    "replay",
    "report",
    "run_day",
    "scan",
    "rebuild_ledger",
    "verify_calendar_truth",
    "score_strategy_evidence",
    "forward_readiness",
    "write_calendar_view",
    "build_trade_blotter",
    "verify_trade_blotter",
    "verify_source_bar_truth",
]
