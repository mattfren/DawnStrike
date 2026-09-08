"""Paper-only broker execution.

Nothing in this package can reach a live trading endpoint: the base URL is
fixed to Alpaca's paper host, every request carries a single-host allowlist,
and the account number is checked for the paper prefix before any order.
"""

from intraday_scanner.execution.paper_broker import (
    LiveTradingRefused,
    PaperBrokerClient,
    PaperBrokerError,
)
from intraday_scanner.execution.paper_engine import (
    EntryPlan,
    PaperExecutionEngine,
    PaperExecutionStore,
    client_order_id,
)
from intraday_scanner.execution.risk_gate import RiskDecision, RiskSettings, evaluate_entry

__all__ = [
    "EntryPlan",
    "LiveTradingRefused",
    "PaperBrokerClient",
    "PaperBrokerError",
    "PaperExecutionEngine",
    "PaperExecutionStore",
    "RiskDecision",
    "RiskSettings",
    "client_order_id",
    "evaluate_entry",
]
