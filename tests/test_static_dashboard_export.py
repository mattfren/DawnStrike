from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intraday_scanner.dashboard import static_dashboard_export as exporter

DAY_ONE = "2026-07-16"
DAY_TWO = "2026-07-17"
RUN_ONE = "paper_ops:forward:2026-07-16:snapshot-one"
RUN_TWO = "paper_ops:forward:2026-07-17:snapshot-two"


def _strategy(
    strategy_id: str,
    *,
    label: str,
    activation: str,
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "strategy_label": label,
        "strategy_version": "v1.0",
        "execution_policy_version": "policy-v1",
        "strategy_semantics_fingerprint": fingerprint,
        "registry_inception_date": activation,
    }


def _paper_row(
    day: str,
    strategy_id: str,
    *,
    start: Decimal,
    pnl: Decimal,
    cumulative: Decimal,
    fingerprint: str,
    run_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    daily_return = pnl / start
    return {
        "date": day,
        "mode": "forward",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "execution_policy_version": "policy-v1",
        "strategy_semantics_fingerprint": fingerprint,
        "daily_return_pct": str(daily_return),
        "cumulative_return_pct": str(cumulative),
        "drawdown_pct": str(min(cumulative, Decimal(0))),
        "realized_pnl": "0",
        "unrealized_pnl": str(pnl),
        "total_pnl": str(pnl),
        "trades_opened": 1 if pnl else 0,
        "trades_closed": 0,
        "open_positions": 1 if pnl else 0,
        "pending_orders": 0,
        "wins": 0,
        "losses": 0,
        "session_open_equity": str(start),
        "run_id": run_id,
        "data_snapshot_id": snapshot_id,
        "series_role": "official",
        "claim_scope": "official_forward",
    }


def _summary(
    day: str,
    rows: list[dict[str, Any]],
    *,
    cumulative: Decimal,
    not_yet_registered: int,
) -> dict[str, Any]:
    start = sum(Decimal(str(row["session_open_equity"])) for row in rows)
    pnl = sum(Decimal(str(row["total_pnl"])) for row in rows)
    return {
        "date": day,
        "fleet_daily_return": str(pnl / start),
        "fleet_cumulative_return": str(cumulative),
        "fleet_daily_pnl": str(pnl),
        "fleet_ending_equity": str(start + pnl),
        "coverage_complete": True,
        "coverage_present": len(rows),
        "coverage_expected": len(rows),
        "coverage_status": "complete",
        "missing_strategy_keys": [],
        "not_yet_registered_strategies": not_yet_registered,
        "positive_strategies": sum(Decimal(str(row["total_pnl"])) > 0 for row in rows),
        "negative_strategies": sum(Decimal(str(row["total_pnl"])) < 0 for row in rows),
        "flat_strategies": sum(Decimal(str(row["total_pnl"])) == 0 for row in rows),
        "missing_strategies": 0,
        "trades_opened": sum(int(row["trades_opened"]) for row in rows),
        "trades_closed": 0,
        "open_positions": sum(int(row["open_positions"]) for row in rows),
        "pending_orders": 0,
        "status": "positive" if pnl > 0 else "negative" if pnl < 0 else "flat",
    }


def _alpha_detail(day: str, *, no_entry: bool) -> dict[str, Any]:
    first = {
        "rank": 1,
        "ticker": "NONE" if no_entry else "GAIN",
        "company": "Public Company",
        "total_score": 64.5,
        "gap_pct": 22.0,
        "trigger": "$10.00",
        "invalidation": "$9.00",
        "target": "$12.00",
        "label/action": "WATCH ONLY",
        "source": "public_provider",
    }
    first_outcome = {
        "rank": 1,
        "ticker": first["ticker"],
        "entry_price": None if no_entry else 10.0,
        "entry_time": "" if no_entry else f"{day}T15:00:00Z",
        "recommended_exit_policy": "not_recorded" if no_entry else "monitor_exit_signal",
        "recommended_exit_price": None if no_entry else 9.875,
        "recommended_exit_return": None if no_entry else -1.25,
        "close_return": None if no_entry else -1.25,
        "monitor_exit_return": None if no_entry else -1.25,
        "high_after_entry_return": None if no_entry else 2.0,
        "low_after_entry_drawdown": None if no_entry else -3.0,
        "audit_status": "resolved_no_entry" if no_entry else "audited",
        "outcome_source": "public_chart",
        "notes": "Trigger never reached." if no_entry else "Audited outcome.",
    }
    picks = [first]
    returns = [first_outcome]
    missing: list[dict[str, Any]] = []
    status = "RESOLVED NO ENTRY" if no_entry else "OUTCOMES PARTIAL"
    if not no_entry:
        picks.append(
            {
                "rank": 2,
                "ticker": "MISS",
                "company": "Missing Outcome Inc.",
                "total_score": 61.0,
                "gap_pct": 18.0,
                "trigger": "$5.00",
                "invalidation": "$4.00",
                "target": "$7.00",
                "label/action": "WATCH ONLY",
                "source": "public_provider",
            }
        )
        returns.append(
            {
                "rank": 2,
                "ticker": "MISS",
                "entry_price": None,
                "entry_time": "",
                "recommended_exit_policy": "not_recorded",
                "recommended_exit_price": None,
                "recommended_exit_return": None,
                "close_return": None,
                "monitor_exit_return": None,
                "high_after_entry_return": None,
                "low_after_entry_drawdown": None,
                "audit_status": "Outcome needed",
                "outcome_source": "none",
                "notes": "Outcome needed before return can be counted.",
            }
        )
        missing.append(
            {
                "ticker": "MISS",
                "rank": 2,
                "audit_status": "Outcome needed",
                "expected_path": "C:/private/outcomes.csv",
            }
        )
    return {
        "date": day,
        "status": status,
        "overview": {
            "date": day,
            "alphaops_decision": "WATCHLIST",
            "model_version": "alpha-v4",
            "source_status": "ok",
            "source_label": "public/free evidence",
        },
        "picks": picks,
        "return_rows": returns,
        "missing_outcomes": missing,
        "telegram": [
            {
                "event_key": "private-event-key",
                "message": "private Telegram message",
            }
        ],
    }


@pytest.fixture
def canonical_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "paper_ops"
    state = root / "state"
    state.mkdir(parents=True)
    (state / "open_positions.json").write_text("[]", encoding="utf-8")
    (state / "pending_orders.json").write_text("[]", encoding="utf-8")
    database = tmp_path / "alpha.sqlite"
    database.write_bytes(b"alpha-database-evidence")

    registry = [
        _strategy("first", label="First Strategy", activation=DAY_ONE, fingerprint="a" * 64),
        _strategy("second", label="Second Strategy", activation=DAY_TWO, fingerprint="b" * 64),
    ]
    day_one_rows = [
        _paper_row(
            DAY_ONE,
            "first",
            start=Decimal("100000"),
            pnl=Decimal("10"),
            cumulative=Decimal("0.0001"),
            fingerprint="a" * 64,
            run_id=RUN_ONE,
            snapshot_id="snapshot-one",
        )
    ]
    day_two_rows = [
        _paper_row(
            DAY_TWO,
            "first",
            start=Decimal("100010"),
            pnl=Decimal("-10"),
            cumulative=Decimal("0"),
            fingerprint="a" * 64,
            run_id=RUN_TWO,
            snapshot_id="snapshot-two",
        ),
        _paper_row(
            DAY_TWO,
            "second",
            start=Decimal("100000"),
            pnl=Decimal("20"),
            cumulative=Decimal("0.0002"),
            fingerprint="b" * 64,
            run_id=RUN_TWO,
            snapshot_id="snapshot-two",
        ),
    ]
    gates = {
        "reconciliation": {"status": "passed"},
        "calendar_truth": {"status": "passed"},
        "ledger_rebuild": {"status": "passed"},
        "trade_blotter": {"status": "passed"},
        "source_bar_truth_forward": {"status": "passed", "mode": "forward"},
    }
    dataset = {
        "status": "verified",
        "source_sha256": "c" * 64,
        "gates": gates,
        "official_series": registry,
        "official_strategy_count": 2,
        "research_only": True,
        "broker_execution_allowed": False,
    }
    view = {
        "status": "verified",
        "truth_status": "verified",
        "mode": "forward",
        "claim_scope": "official_forward",
        "latest_date": DAY_TWO,
        "dates": [DAY_ONE, DAY_TWO],
        "official_rows": day_one_rows + day_two_rows,
        "day_summaries": [
            _summary(
                DAY_ONE,
                day_one_rows,
                cumulative=Decimal("0.0001"),
                not_yet_registered=1,
            ),
            _summary(
                DAY_TWO,
                day_two_rows,
                cumulative=Decimal("0.000099995"),
                not_yet_registered=0,
            ),
        ],
        "unknown_rows": [],
        "impossible_forward_rows": [],
        "blotter_rows": [],
        "blotter_verified": True,
        "research_only": True,
        "broker_execution_allowed": False,
    }
    alpha = {
        DAY_ONE: _alpha_detail(DAY_ONE, no_entry=True),
        DAY_TWO: _alpha_detail(DAY_TWO, no_entry=False),
    }
    monkeypatch.setattr(exporter, "load_paper_ops_calendar", lambda _root: dataset)
    monkeypatch.setattr(
        exporter, "build_paper_ops_calendar_view", lambda _dataset, _mode: view
    )
    monkeypatch.setattr(
        exporter,
        "load_calendar_day_detail",
        lambda _database, day: alpha[str(day)],
    )
    return {
        "root": root,
        "database": database,
        "dataset": dataset,
        "view": view,
        "alpha": alpha,
    }


def _build(sources: dict[str, Any]) -> dict[str, Any]:
    return exporter.build_static_dashboard_payload(
        paper_ops_root=sources["root"],
        database_path=sources["database"],
        generated_at=datetime(2026, 7, 17, 23, 0, tzinfo=timezone.utc),
    )


def test_builds_v3_payload_with_truthful_paper_and_alpha_history(
    canonical_sources: dict[str, Any],
) -> None:
    payload = _build(canonical_sources)

    assert payload["schemaVersion"] == exporter.OUTPUT_SCHEMA
    assert payload["latestRunDate"] == DAY_TWO
    assert payload["sourceObservedDates"] == [DAY_ONE, DAY_TWO]
    assert payload["freshness"] == {
        "asOfDate": DAY_TWO,
        "expectedNextSessionDate": "2026-07-20",
        "deadlineAt": "2026-07-20T22:00:00Z",
        "statusAtGeneration": "fresh",
    }
    assert {"topMetrics", "operatorWatchlist", "calendar", "strategies"} <= payload.keys()

    paper_days = payload["paperOps"]["days"]
    assert [row["date"] for row in paper_days] == [DAY_ONE, DAY_TWO]
    assert paper_days[1]["coveragePresent"] == 2
    assert paper_days[1]["coverageExpected"] == 2
    assert Decimal(paper_days[1]["fleetDailyPnl"]) == Decimal("10")
    assert Decimal(paper_days[1]["fleetDailyReturnFraction"]) == Decimal("10") / Decimal(
        "200010"
    )

    alpha_days = payload["alphaOps"]["days"]
    assert alpha_days[0]["status"] == "RESOLVED NO ENTRY"
    assert alpha_days[0]["outcomeCoverage"] == {
        "eligible": 1,
        "audited": 0,
        "resolvedNoEntry": 1,
        "missing": 0,
        "complete": True,
    }
    assert alpha_days[0]["picks"][0]["outcome"]["closeReturnPct"] is None
    assert alpha_days[1]["outcomeCoverage"]["missing"] == 1
    assert alpha_days[1]["picks"][1]["outcome"]["recommendedExitReturnPct"] is None


def test_public_payload_whitelists_paths_messages_and_private_ids(
    canonical_sources: dict[str, Any],
) -> None:
    canonical_sources["alpha"][DAY_TWO]["return_rows"][0]["notes"] = (
        "telegram_bot_token=SECRET_DO_NOT_PUBLISH"
    )
    payload = _build(canonical_sources)
    rendered = json.dumps(payload)

    assert str(canonical_sources["root"]) not in rendered
    assert str(canonical_sources["database"]) not in rendered
    assert "C:/private/outcomes.csv" not in rendered
    assert "private-event-key" not in rendered
    assert "private Telegram message" not in rendered
    assert "SECRET_DO_NOT_PUBLISH" not in rendered
    assert "Audited sourced outcome." in rendered
    assert "expected_path" not in rendered


def test_blocks_nonfinite_values_from_public_json(
    canonical_sources: dict[str, Any],
) -> None:
    canonical_sources["alpha"][DAY_TWO]["return_rows"][0]["close_return"] = float(
        "nan"
    )

    with pytest.raises(exporter.StaticDashboardExportError, match="non-JSON-safe"):
        _build(canonical_sources)


def test_evidence_hashes_bind_calendar_database_registry_and_public_payload(
    canonical_sources: dict[str, Any],
) -> None:
    payload = _build(canonical_sources)
    evidence = payload["evidence"]

    assert evidence["paperOpsCalendarSha256"] == "c" * 64
    assert evidence["alphaDatabaseSha256"] == hashlib.sha256(
        b"alpha-database-evidence"
    ).hexdigest()
    assert len(evidence["paperOpsRegistrySha256"]) == 64
    assert len(evidence["alphaPublicDaysSha256"]) == 64
    assert len(evidence["publicPayloadSha256"]) == 64
    assert evidence["paperOpsRunIds"] == [RUN_ONE, RUN_TWO]
    detached = copy.deepcopy(payload)
    detached["evidence"].pop("publicPayloadSha256")
    assert evidence["publicPayloadSha256"] == exporter._sha256_json(detached)


@pytest.mark.parametrize(
    ("gate", "message"),
    [
        ("calendar_truth", "calendar_truth gate is not passed"),
        ("reconciliation", "reconciliation gate is not passed"),
        ("trade_blotter", "trade_blotter gate is not passed"),
    ],
)
def test_blocks_publication_when_a_paper_truth_gate_fails(
    canonical_sources: dict[str, Any], gate: str, message: str
) -> None:
    canonical_sources["dataset"]["gates"][gate]["status"] = "failed"

    with pytest.raises(exporter.StaticDashboardExportError, match=message):
        _build(canonical_sources)


def test_blocks_publication_when_latest_strategy_coverage_is_incomplete(
    canonical_sources: dict[str, Any],
) -> None:
    canonical_sources["view"]["day_summaries"][-1]["coverage_complete"] = False

    with pytest.raises(exporter.StaticDashboardExportError, match="coverage is incomplete"):
        _build(canonical_sources)


def test_atomic_export_preserves_prior_asset_after_source_failure(
    canonical_sources: dict[str, Any], tmp_path: Path
) -> None:
    output = tmp_path / "assets" / "dashboard-data.json"
    payload = exporter.export_static_dashboard(
        paper_ops_root=canonical_sources["root"],
        database_path=canonical_sources["database"],
        output_path=output,
        generated_at=datetime(2026, 7, 17, 23, 0, tzinfo=timezone.utc),
    )
    original = output.read_bytes()
    assert json.loads(original)["schemaVersion"] == exporter.OUTPUT_SCHEMA
    assert payload["latestRunDate"] == DAY_TWO

    canonical_sources["dataset"]["gates"]["calendar_truth"]["status"] = "failed"
    with pytest.raises(exporter.StaticDashboardExportError):
        exporter.export_static_dashboard(
            paper_ops_root=canonical_sources["root"],
            database_path=canonical_sources["database"],
            output_path=output,
        )

    assert output.read_bytes() == original
    assert list(output.parent.glob("dashboard-data-*.tmp")) == []


def test_historical_date_is_explicitly_stale_and_does_not_include_future_rows(
    canonical_sources: dict[str, Any],
) -> None:
    payload = exporter.build_static_dashboard_payload(
        paper_ops_root=canonical_sources["root"],
        database_path=canonical_sources["database"],
        run_date=DAY_ONE,
        generated_at=datetime(2026, 7, 16, 23, 0, tzinfo=timezone.utc),
    )

    assert payload["sourceObservedDates"] == [DAY_ONE]
    assert payload["freshness"]["statusAtGeneration"] == "stale"
    assert [row["date"] for row in payload["paperOps"]["days"]] == [DAY_ONE]
    assert [row["date"] for row in payload["alphaOps"]["days"]] == [DAY_ONE]


def test_naive_generation_timestamp_is_rejected(canonical_sources: dict[str, Any]) -> None:
    with pytest.raises(exporter.StaticDashboardExportError, match="include a timezone"):
        exporter.build_static_dashboard_payload(
            paper_ops_root=canonical_sources["root"],
            database_path=canonical_sources["database"],
            generated_at=datetime(2026, 7, 17, 23, 0),
        )
