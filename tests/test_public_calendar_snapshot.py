from __future__ import annotations

import json
from pathlib import Path

import pytest

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.performance.calendar_snapshot import (
    _calendar_status,
    build_calendar_payload,
    write_public_calendar,
)
from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_calendar_copies_canonical_values_and_compounds_monthly_returns() -> None:
    performance = _performance(
        [
            _daily("2026-08-03", return_pct=10.0, status="COMPLETE"),
            _daily("2026-08-04", return_pct=-10.0, status="COMPLETE"),
        ],
        as_of="2026-08-04",
    )

    payload = build_calendar_payload(
        performance,
        as_of_market_date="2026-08-04",
        generated_at="2026-08-04T22:00:00+00:00",
    )

    august_third = _record(payload, "2026-08-03")
    assert august_third["net_return_pct"] == 10.0
    assert august_third["gross_return_pct"] == 10.2
    assert august_third["benchmark_return_pct"] == 0.5
    monthly = _month(payload, "2026-08")
    assert monthly["eligible_day_count"] == 2
    assert monthly["expected_market_day_count"] == 2
    assert monthly["coverage_pct"] == 100.0
    assert monthly["net_return_pct"] == pytest.approx(-1.0)


def test_calendar_keeps_pending_null_and_counts_it_in_denominator() -> None:
    pending = _daily("2026-08-04", return_pct=None, status="PARTIAL")
    pending["missing_outcome_count"] = 1
    pending["opening_equity_cents"] = None
    pending["ending_equity_cents"] = None
    performance = _performance(
        [
            _daily("2026-08-03", return_pct=0.0, status="NO_TRADE"),
            pending,
        ],
        as_of="2026-08-04",
    )

    payload = build_calendar_payload(performance, as_of_market_date="2026-08-04")

    no_trade = _record(payload, "2026-08-03")
    pending_record = _record(payload, "2026-08-04")
    assert no_trade["observed_zero"] is True
    assert no_trade["net_return_pct"] == 0.0
    assert pending_record["status"] == "PENDING"
    assert pending_record["net_return_pct"] is None
    assert pending_record["observed_zero"] is False
    assert "1 outcome(s) missing" in pending_record["missing_reasons"]
    monthly = _month(payload, "2026-08")
    assert monthly["eligible_day_count"] == 1
    assert monthly["expected_market_day_count"] == 2
    assert monthly["missing_or_ineligible_day_count"] == 1
    assert monthly["coverage_pct"] == 50.0
    assert monthly["net_return_pct"] == 0.0


def test_calendar_publishes_research_return_without_promoting_it_to_official() -> None:
    historical = _daily("2026-07-06", return_pct=1.25, status="PARTIAL")
    historical["cohort"] = "historical_backtest"
    historical["account_id"] = "historical_replay"
    historical["opening_equity_cents"] = None
    historical["ending_equity_cents"] = None
    performance = _performance([historical], as_of="2026-07-06")

    payload = build_calendar_payload(performance, as_of_market_date="2026-07-06")

    record = _record(payload, "2026-07-06")
    assert record["eligible_for_return"] is False
    assert record["net_return_pct"] is None
    assert record["research_return_pct"] == 1.25
    assert record["return_display_state"] == "research_only_observed"
    monthly = _month(payload, "2026-07")
    assert monthly["net_return_pct"] is None
    assert monthly["research_observed_return_pct"] == 1.25
    assert monthly["research_observed_day_count"] == 1
    assert monthly["research_observed_coverage_pct"] == 100.0
    assert monthly["research_missing_day_count"] == 0
    assert monthly["return_scope"] == "research_only"


def test_calendar_closed_day_is_unavailable_and_never_observed() -> None:
    payload = build_calendar_payload(
        _performance([], as_of="2026-08-02"),
        as_of_market_date="2026-08-02",
    )

    saturday = next(day for day in payload["days"] if day["date"] == "2026-08-01")
    assert saturday["market_session_status"] == "closed"
    assert saturday["status"] == "UNAVAILABLE"
    assert saturday["observed"] is False
    assert saturday["observed_zero"] is False


def test_calendar_writer_binds_manifest_to_canonical_input(tmp_path: Path) -> None:
    db_path = tmp_path / "calendar.sqlite"
    SQLiteScanStore(db_path).initialize()
    CanonicalPerformanceService(db_path).reconcile(
        market_date="2026-07-31",
        now="2026-07-31T22:00:00+00:00",
    )
    output_path = tmp_path / "public" / "data" / "calendar.json"

    publication = write_public_calendar(
        db_path,
        output_path,
        market_date="2026-07-31",
        canonical_input_hash_sha256="canonical-hash",
        performance_payload_sha256="performance-hash",
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    manifest = publication["manifest"]
    assert payload["canonical_input_hash_sha256"] == "canonical-hash"
    assert payload["performance_payload_sha256"] == "performance-hash"
    assert manifest["canonical_input_hash_sha256"] == "canonical-hash"
    assert manifest["performance_payload_sha256"] == "performance-hash"
    assert manifest["status"] == "degraded"
    assert Path(publication["manifest_path"]).exists()


def test_calendar_readiness_is_scoped_to_official_forward_paper() -> None:
    payload = {
        "days": [
            {
                "date": "2026-08-04",
                "records": [
                    {"cohort": "official_forward_paper", "status": "NO_TRADE"},
                    {"cohort": "shadow_challenger", "status": "UNREALIZED"},
                ],
            }
        ]
    }

    assert _calendar_status(payload, "2026-08-04") == "no_trade"
    payload["days"][0]["records"][0]["status"] = "PENDING"
    assert _calendar_status(payload, "2026-08-04") == "degraded"


def _performance(
    daily: list[dict[str, object]],
    *,
    as_of: str,
) -> dict[str, object]:
    return {
        "as_of_market_date": as_of,
        "daily": daily,
        "rows": [],
        "accounts": [
            {
                "account_id": ALPHAOPS_V5_ACCOUNT_ID,
                "activation_timestamp": "2026-07-31T00:00:00-04:00",
            }
        ],
        "account_ledger": [
            {
                "account_id": ALPHAOPS_V5_ACCOUNT_ID,
                "market_date": row["market_date"],
                "observed_zero": row["status"] == "NO_TRADE",
            }
            for row in daily
        ],
    }


def _daily(
    market_date: str,
    *,
    return_pct: float | None,
    status: str,
) -> dict[str, object]:
    return {
        "performance_id": f"v5:{market_date}",
        "market_date": market_date,
        "cohort": "official_forward_paper",
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "execution_policy_version": ALPHAOPS_V5_POLICY_VERSION,
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "status": status,
        "evidence_state": status.lower(),
        "return_pct": return_pct,
        "gross_return_pct": None if return_pct is None else return_pct + 0.2,
        "benchmark_return_pct": None if return_pct is None else 0.5,
        "excess_return_pct": None if return_pct is None else return_pct - 0.5,
        "cumulative_return_pct": return_pct,
        "drawdown_pct": 0.0 if return_pct is not None else None,
        "gross_pnl_cents": None if return_pct is None else int((return_pct + 0.2) * 1000),
        "fees_cents": None if return_pct is None else 100,
        "slippage_cents": None if return_pct is None else 100,
        "net_pnl_cents": None if return_pct is None else int(return_pct * 1000),
        "opening_equity_cents": 10_000_000,
        "external_flow_cents": 0,
        "ending_equity_cents": (
            None if return_pct is None else 10_000_000 + int(return_pct * 1000)
        ),
        "cash_cents": (
            None if return_pct is None else 10_000_000 + int(return_pct * 1000)
        ),
        "position_market_value_cents": 0 if return_pct is not None else None,
        "accounting_delta_cents": 0 if return_pct is not None else None,
        "realized_trade_count": 0 if status == "NO_TRADE" else 1,
        "unrealized_trade_count": 0,
        "missing_outcome_count": 0,
        "quarantined_count": 0,
        "no_trade_count": 1 if status == "NO_TRADE" else 0,
        "coverage": {
            "eligible_count": 0 if status == "NO_TRADE" else 1,
            "observed_count": 0 if status == "NO_TRADE" else 1,
            "missing_count": 0,
            "excluded_count": 0,
            "coverage_pct": 100.0,
        },
        "source_refs": ["source"],
        "input_hash_sha256": "canonical-row-hash",
        "calculation_version": "dawnstrike-performance-v2",
        "return_basis": "account_equity_identity_after_external_flows",
        "cost_status": "complete",
    }


def _record(payload: dict[str, object], market_date: str) -> dict[str, object]:
    day = next(row for row in payload["days"] if row["date"] == market_date)
    return day["records"][0]


def _month(payload: dict[str, object], month: str) -> dict[str, object]:
    return next(row for row in payload["months"] if row["month"] == month)
