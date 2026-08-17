"""Operation-local verification state for discovery-metric persistence."""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from intraday_scanner.storage.opportunity_metric_errors import (
    OpportunityMetricIntegrityError,
)
from intraday_scanner.v2.opportunity.miss_metric_persistence import (
    CurrentSessionMetricReplay,
    HistoricalMetricReplay,
)
from intraday_scanner.v2.opportunity.miss_metric_reconciliation import (
    SessionDiscoveryMetricReport,
)
from intraday_scanner.v2.opportunity.miss_persistence import (
    CurrentMissReplay,
    HistoricalMissReplay,
)
from intraday_scanner.v2.opportunity.models import stable_identity


class _MetricVerificationContext:
    """Private caches scoped to one SQLite connection and transaction."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self.metric_chains: dict[str, Any] = {}
        self.metric_receipts: dict[str, Any] = {}
        self.historical_metric_replays: dict[str, Any] = {}
        self.miss_chains: dict[str, Any] = {}
        self.historical_miss_replays: dict[str, Any] = {}
        self.current_miss_replays: dict[tuple[Any, ...], Any] = {}
        self.current_session_replays: dict[tuple[str, str, str], Any] = {}
        self.current_miss_parent_results: dict[tuple[Any, ...], Any] = {}
        self.in_progress: dict[str, set[Any]] = {
            "metric_chain": set(),
            "metric_receipt": set(),
            "historical_metric": set(),
            "miss_chain": set(),
            "historical_miss": set(),
            "current_miss": set(),
            "current_session": set(),
            "current_miss_parent": set(),
        }

    def assert_connection(self, connection: sqlite3.Connection) -> None:
        if connection is not self._connection:
            raise OpportunityMetricIntegrityError(
                "metric verification context crossed SQLite connections"
            )

    def enter(self, family: str, key: Any) -> None:
        active = self.in_progress[family]
        if key in active:
            raise OpportunityMetricIntegrityError(
                f"cycle in persisted metric verification: {family}"
            )
        active.add(key)

    def leave(self, family: str, key: Any) -> None:
        self.in_progress[family].discard(key)


def _current_shape_from_historical(historical: HistoricalMissReplay) -> CurrentMissReplay:
    values: dict[str, Any] = {
        "miss_persistence_receipt": historical.miss_persistence_receipt,
        "miss_batch": historical.miss_batch,
        "full_chain_receipts": historical.chain_prefix_receipts,
        "full_chain_batches": historical.chain_prefix_batches,
        "current_parent_outcome_replays": historical.parent_outcome_replays,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.current_miss_replay.v1",
    }
    return CurrentMissReplay(
        replay_id=stable_identity("current-opportunity-miss-replay", values),
        **values,
    )


def _current_shape_from_historical_metric(
    historical: HistoricalMetricReplay,
) -> CurrentSessionMetricReplay:
    if not isinstance(historical.metric_report, SessionDiscoveryMetricReport):
        raise OpportunityMetricIntegrityError(
            "multi binding child must be a SESSION metric report"
        )
    miss = cast(HistoricalMissReplay, historical.historical_miss_replay)
    current_miss = _current_shape_from_historical(miss)
    values: dict[str, Any] = {
        "metric_persistence_receipt": historical.metric_persistence_receipt,
        "metric_report": historical.metric_report,
        "full_chain_receipts": historical.chain_prefix_receipts,
        "full_chain_reports": historical.chain_prefix_reports,
        "current_miss_replay": current_miss,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.current_session_metric_replay.v1",
    }
    return CurrentSessionMetricReplay(
        replay_id=stable_identity(
            "current-session-opportunity-metric-replay",
            values,
        ),
        **values,
    )


def _embedded_outcome_head_key(batch: Any) -> tuple[Any, ...]:
    return tuple(
        (
            item.pipeline_result.run_id,
            item.pipeline_result.content_hash(),
            item.outcome_persistence_receipt.outcome_receipt_id,
            item.outcome_persistence_receipt.content_hash(),
            item.replay_id,
            item.content_hash(),
        )
        for item in batch.session_replay.current_outcome_replays
    )


__all__: list[str] = []
