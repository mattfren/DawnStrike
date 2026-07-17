from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from intraday_scanner.dashboard.paper_ops_calendar_service import (
    PaperOpsCalendarError,
    build_paper_ops_calendar_view,
    format_return_fraction,
    load_paper_ops_calendar,
)

CALENDAR_FIELDS = (
    "date",
    "mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
    "data_snapshot_id",
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
    "average_r",
    "expectancy_r",
    "exposure_pct",
    "fees_paid",
    "slippage_estimate",
    "warnings",
    "run_id",
)


def _row(**overrides: object) -> dict[str, str]:
    values: dict[str, object] = {
        "date": "2026-07-14",
        "mode": "replay",
        "strategy_id": "alpha",
        "strategy_version": "v1.0",
        "strategy_status": "official",
        "execution_policy_version": "paper-policy-v1",
        "strategy_semantics_fingerprint": "alpha-fingerprint-v1",
        "data_snapshot_id": "snapshot-2026-07-14",
        "starting_equity": "1000",
        "ending_equity": "1010",
        "realized_pnl": "10",
        "unrealized_pnl": "0",
        "total_pnl": "10",
        "daily_return_pct": "0.01",
        "cumulative_return_pct": "0.01",
        "drawdown_pct": "0",
        "trades_opened": "1",
        "trades_closed": "1",
        "pending_orders": "0",
        "open_positions": "0",
        "wins": "1",
        "losses": "0",
        "flats": "0",
        "average_r": "1",
        "expectancy_r": "1",
        "exposure_pct": "0",
        "fees_paid": "0.25",
        "slippage_estimate": "0.10",
        "warnings": "",
        "run_id": "paperops-replay-20260714",
    }
    values.update(overrides)
    return {field: "" if values[field] is None else str(values[field]) for field in CALENDAR_FIELDS}


def _official(
    strategy_id: str,
    strategy_version: str,
    fingerprint: str,
    *,
    policy: str = "paper-policy-v1",
) -> dict[str, str]:
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_status": "official",
        "execution_policy_version": policy,
        "strategy_semantics_fingerprint": fingerprint,
    }


def _challenger(
    strategy_id: str,
    strategy_version: str,
    fingerprint: str,
    *,
    policy: str = "paper-policy-v1",
) -> dict[str, str]:
    return {
        "challenger_id": f"{strategy_id}-{strategy_version}-shadow",
        "strategy_id": strategy_id,
        "candidate_strategy_version": strategy_version,
        "execution_policy_version": policy,
        "candidate_strategy_semantics_fingerprint": fingerprint,
        "status": "shadow",
    }


@dataclass(frozen=True)
class PaperOpsFixture:
    root: Path
    calendar_path: Path
    gate_paths: dict[str, Path]


def _write_fixture(
    root: Path,
    rows: list[dict[str, str]],
    *,
    officials: list[dict[str, str]],
    challengers: list[dict[str, str]] | None = None,
    gate_statuses: dict[str, str] | None = None,
    strategy_registered_at: dict[tuple[str, str], str] | None = None,
    policy_registered_at: dict[str, str] | None = None,
    strategy_activation_policy: dict[tuple[str, str], str] | None = None,
    policy_activation_policy: dict[str, str] | None = None,
) -> PaperOpsFixture:
    calendar_path = root / "calendar" / "strategy_daily_returns.csv"
    state = root / "state"
    reconciliation = root / "reconciliation"
    exports = root / "exports"
    for directory in (calendar_path.parent, state, reconciliation, exports):
        directory.mkdir(parents=True, exist_ok=True)

    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CALENDAR_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    (state / "strategy_registry.json").write_text(
        json.dumps(officials, indent=2),
        encoding="utf-8",
    )
    default_registered_at = "2026-07-01T12:00:00+00:00"
    semantics: dict[str, dict[str, object]] = {}
    for official in officials:
        strategy_id = official["strategy_id"]
        strategy_version = official["strategy_version"]
        fingerprint = official["strategy_semantics_fingerprint"]
        semantics_key = f"{strategy_id}@{strategy_version}"
        existing = semantics.get(semantics_key)
        if existing is not None and existing["fingerprint"] != fingerprint:
            raise AssertionError(
                "A strategy version cannot carry multiple semantics fingerprints."
            )
        semantics_entry: dict[str, object] = {
            "configuration": {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
            },
            "fingerprint": fingerprint,
            "registered_at": (strategy_registered_at or {}).get(
                (strategy_id, strategy_version), default_registered_at
            ),
        }
        activation_policy = (strategy_activation_policy or {}).get(
            (strategy_id, strategy_version)
        )
        if activation_policy is not None:
            semantics_entry["activation_policy"] = activation_policy
        semantics[semantics_key] = semantics_entry
    (state / "strategy_semantics_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "v2.strategy_semantics_manifest.v1",
                "strategies": semantics,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    policies: dict[str, dict[str, object]] = {}
    for official in officials:
        policy_version = official["execution_policy_version"]
        policy_entry: dict[str, object] = {
            "configuration": {"test_fixture": True},
            "fingerprint": f"{policy_version}-fingerprint",
            "registered_at": (policy_registered_at or {}).get(
                policy_version, default_registered_at
            ),
        }
        activation_policy = (policy_activation_policy or {}).get(policy_version)
        if activation_policy is not None:
            policy_entry["activation_policy"] = activation_policy
        policies[policy_version] = policy_entry
    (state / "execution_policy_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "v2.paper_execution_policy_manifest.v1",
                "active_execution_policy_version": (
                    officials[0]["execution_policy_version"] if officials else ""
                ),
                "policies": policies,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (state / "strategy_challenger_registry.json").write_text(
        json.dumps(
            {
                "schema_version": "v2.paper_ops_challenger_registry.v1",
                "challengers": challengers or [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (exports / "paper_trade_blotter.json").write_text(
        json.dumps(
            {
                "schema_version": "v2.paper_trade_blotter.v1",
                "status": "passed",
                "mode": "replay",
                "row_count": 0,
                "rows": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    requested_statuses = gate_statuses or {}
    modes = sorted({row["mode"] for row in rows})
    gate_payloads: dict[str, dict[str, Any]] = {
        "reconciliation": {
            "schema_version": "v2.paper_ops_reconciliation.v1",
            "status": requested_statuses.get("reconciliation", "passed"),
        },
        "calendar_truth": {
            "schema_version": "v2.paper_ops_calendar_truth.v2",
            "status": requested_statuses.get("calendar_truth", "passed"),
        },
        "ledger_rebuild": {
            "schema_version": "v2.paper_ops_ledger_rebuild.v1",
            "status": requested_statuses.get("ledger_rebuild", "passed"),
        },
        "trade_blotter": {
            "schema_version": "v2.paper_trade_blotter_verify.v1",
            "status": requested_statuses.get("trade_blotter", "passed"),
            "row_count": 0,
            "source_bar_truth": {"mode": modes[0]},
        },
    }
    for mode in modes:
        key = f"source_bar_truth_{mode}"
        gate_payloads[key] = {
            "schema_version": "v2.paper_ops_source_bar_truth.v1",
            "status": requested_statuses.get(key, "passed"),
            "mode": mode,
        }

    filenames = {
        "reconciliation": "reconciliation_latest.json",
        "calendar_truth": "calendar_truth_latest.json",
        "ledger_rebuild": "ledger_rebuild_latest.json",
        "trade_blotter": "trade_blotter_verify_latest.json",
        **{
            f"source_bar_truth_{mode}": f"source_bar_truth_{mode}_latest.json"
            for mode in modes
        },
    }
    gate_paths: dict[str, Path] = {}
    for name, payload in gate_payloads.items():
        path = reconciliation / filenames[name]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        gate_paths[name] = path

    # Explicit mtimes make freshness deterministic even on coarse-resolution filesystems.
    baseline = time.time() - 120
    os.utime(calendar_path, (baseline, baseline))
    for path in gate_paths.values():
        os.utime(path, (baseline + 60, baseline + 60))
    return PaperOpsFixture(root=root, calendar_path=calendar_path, gate_paths=gate_paths)


def _summary_for(view: dict[str, Any], session_date: str) -> dict[str, Any]:
    return next(row for row in view["day_summaries"] if row["date"] == session_date)


def test_fleet_returns_use_exact_official_equity_math_and_exclude_other_roles(
    tmp_path: Path,
) -> None:
    rows = [
        _row(strategy_id="alpha", strategy_semantics_fingerprint="alpha-v1"),
        _row(
            strategy_id="beta",
            strategy_semantics_fingerprint="beta-v1",
            ending_equity="980",
            realized_pnl="-20",
            total_pnl="-20",
            daily_return_pct="-0.02",
            cumulative_return_pct="-0.02",
            losses="1",
            wins="0",
        ),
        _row(
            strategy_id="alpha",
            strategy_version="v2.0",
            strategy_status="shadow",
            strategy_semantics_fingerprint="alpha-shadow-v2",
            ending_equity="2000",
            realized_pnl="1000",
            total_pnl="1000",
            daily_return_pct="1",
            cumulative_return_pct="1",
        ),
        _row(
            strategy_id="benchmark_buy_hold_equal_weight",
            strategy_status="benchmark",
            strategy_semantics_fingerprint="benchmark-v1",
            ending_equity="1900",
            realized_pnl="900",
            total_pnl="900",
            daily_return_pct="0.9",
            cumulative_return_pct="0.9",
        ),
        _row(
            strategy_id="cash_no_trade_baseline",
            strategy_status="baseline",
            strategy_semantics_fingerprint="cash-v1",
            ending_equity="1000",
            realized_pnl="0",
            total_pnl="0",
            daily_return_pct="0",
            cumulative_return_pct="0",
            trades_opened="0",
            trades_closed="0",
            wins="0",
        ),
    ]
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        rows,
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("beta", "v1.0", "beta-v1"),
        ],
        challengers=[_challenger("alpha", "v2.0", "alpha-shadow-v2")],
    )

    dataset = load_paper_ops_calendar(fixture.root)
    view = build_paper_ops_calendar_view(dataset, "replay")
    day = _summary_for(view, "2026-07-14")

    assert dataset["status"] == "verified"
    assert view["status"] == "verified"
    assert len(view["official_rows"]) == 2
    assert len(view["challenger_rows"]) == 1
    assert len(view["benchmark_rows"]) == 1
    assert len(view["cash_rows"]) == 1
    assert day["coverage_complete"] is True
    assert day["fleet_daily_pnl"] == Decimal("-10")
    assert day["fleet_ending_equity"] == Decimal("1990")
    assert day["fleet_daily_return"] == Decimal("-0.005")
    assert day["fleet_cumulative_return"] == Decimal("-0.005")
    assert day["benchmark_daily_return"] == Decimal("0.9")
    assert day["cash_daily_return"] == Decimal("0")
    assert format_return_fraction(day["fleet_daily_return"]) == "-0.50%"


def test_modes_and_full_strategy_identities_never_blend(tmp_path: Path) -> None:
    rows = [
        _row(
            mode="replay",
            strategy_id="alpha",
            strategy_version="v1.0",
            strategy_semantics_fingerprint="alpha-v1",
        ),
        _row(
            mode="replay",
            strategy_id="alpha",
            strategy_version="v2.0",
            strategy_semantics_fingerprint="alpha-v2",
            ending_equity="1020",
            total_pnl="20",
            daily_return_pct="0.02",
            cumulative_return_pct="0.02",
        ),
        _row(
            mode="replay",
            strategy_id="alpha",
            strategy_version="v1.0",
            execution_policy_version="paper-policy-v2",
            strategy_semantics_fingerprint="alpha-v1",
            ending_equity="1030",
            total_pnl="30",
            daily_return_pct="0.03",
            cumulative_return_pct="0.03",
        ),
        _row(
            mode="forward",
            strategy_id="alpha",
            strategy_version="v1.0",
            strategy_semantics_fingerprint="alpha-v1",
            ending_equity="970",
            total_pnl="-30",
            daily_return_pct="-0.03",
            cumulative_return_pct="-0.03",
        ),
    ]
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        rows,
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("alpha", "v2.0", "alpha-v2"),
            _official(
                "alpha",
                "v1.0",
                "alpha-v1",
                policy="paper-policy-v2",
            ),
        ],
        strategy_registered_at={
            ("alpha", "v1.0"): "2026-07-01T12:00:00+00:00",
            ("alpha", "v2.0"): "2026-07-15T12:00:00+00:00",
        },
        policy_registered_at={
            "paper-policy-v1": "2026-07-01T12:00:00+00:00",
            "paper-policy-v2": "2026-07-15T12:00:00+00:00",
        },
    )

    dataset = load_paper_ops_calendar(fixture.root)
    replay = build_paper_ops_calendar_view(dataset, "replay")
    forward = build_paper_ops_calendar_view(dataset, "forward")

    assert dataset["available_modes"] == ["forward", "replay"]
    assert {row["mode"] for row in replay["rows"]} == {"replay"}
    assert {row["mode"] for row in forward["rows"]} == {"forward"}
    replay_keys = {row["series_key"] for row in replay["official_rows"]}
    forward_keys = {row["series_key"] for row in forward["official_rows"]}
    assert len(replay_keys) == 3
    assert len(forward_keys) == 1
    assert replay_keys.isdisjoint(forward_keys)
    assert {row["strategy_version"] for row in replay["strategy_summaries"]} == {
        "v1.0",
        "v2.0",
    }
    assert {
        (
            row["strategy_version"],
            row["execution_policy_version"],
            row["strategy_semantics_fingerprint"],
        )
        for row in replay["strategy_summaries"]
    } == {
        ("v1.0", "paper-policy-v1", "alpha-v1"),
        ("v1.0", "paper-policy-v2", "alpha-v1"),
        ("v2.0", "paper-policy-v1", "alpha-v2"),
    }
    assert _summary_for(replay, "2026-07-14")["fleet_daily_return"] == Decimal(
        "0.02"
    )
    assert _summary_for(forward, "2026-07-14")["fleet_daily_return"] == Decimal(
        "-0.03"
    )


def test_forward_coverage_is_registry_inception_aware(tmp_path: Path) -> None:
    rows = [
        _row(
            date="2026-07-14",
            mode="forward",
            strategy_id="alpha",
            strategy_semantics_fingerprint="alpha-v1",
        ),
        _row(
            date="2026-07-15",
            mode="forward",
            strategy_id="alpha",
            strategy_semantics_fingerprint="alpha-v1",
        ),
        _row(
            date="2026-07-15",
            mode="forward",
            strategy_id="beta",
            strategy_semantics_fingerprint="beta-v1",
        ),
    ]
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        rows,
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("beta", "v1.0", "beta-v1"),
        ],
        strategy_registered_at={
            ("alpha", "v1.0"): "2026-07-01T12:00:00+00:00",
            ("beta", "v1.0"): "2026-07-15T12:00:00+00:00",
        },
    )

    view = build_paper_ops_calendar_view(load_paper_ops_calendar(fixture.root), "forward")
    before = _summary_for(view, "2026-07-14")
    inception = _summary_for(view, "2026-07-15")

    assert before["coverage_complete"] is True
    assert before["coverage_expected"] == 1
    assert before["coverage_present"] == 1
    assert before["missing_strategies"] == 0
    assert before["not_yet_registered_strategies"] == 1
    assert before["fleet_daily_return"] == Decimal("0.01")
    assert before["not_yet_registered_strategy_keys"] == [
        "forward|beta|v1.0|paper-policy-v1|beta-v1"
    ]
    assert inception["coverage_complete"] is True
    assert inception["coverage_expected"] == 2
    assert inception["not_yet_registered_strategies"] == 0


def test_forward_requires_exact_evidence_from_registry_inception(tmp_path: Path) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [
            _row(
                date="2026-07-15",
                mode="forward",
                strategy_id="alpha",
                strategy_semantics_fingerprint="alpha-v1",
            )
        ],
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("beta", "v1.0", "beta-v1"),
        ],
        strategy_registered_at={
            ("alpha", "v1.0"): "2026-07-01T12:00:00+00:00",
            ("beta", "v1.0"): "2026-07-15T12:00:00+00:00",
        },
    )

    view = build_paper_ops_calendar_view(load_paper_ops_calendar(fixture.root), "forward")
    day = _summary_for(view, "2026-07-15")
    beta = next(row for row in view["strategy_summaries"] if row["strategy_id"] == "beta")

    assert day["coverage_complete"] is False
    assert day["coverage_status"] == "missing"
    assert day["coverage_expected"] == 2
    assert day["coverage_present"] == 1
    assert day["missing_strategies"] == 1
    assert day["missing_strategy_keys"] == [
        "forward|beta|v1.0|paper-policy-v1|beta-v1"
    ]
    assert day["fleet_daily_return"] is None
    assert beta["registration_status"] == "missing"
    assert beta["period_return"] is None


def test_after_close_registration_begins_on_next_market_session(tmp_path: Path) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [
            _row(
                date="2026-07-16",
                mode="forward",
                strategy_id="alpha",
                strategy_semantics_fingerprint="alpha-v1",
            )
        ],
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("beta", "v1.0", "beta-v1"),
        ],
        strategy_registered_at={
            ("alpha", "v1.0"): "2026-07-01T12:00:00+00:00",
            ("beta", "v1.0"): "2026-07-16T20:01:00+00:00",
        },
    )

    view = build_paper_ops_calendar_view(load_paper_ops_calendar(fixture.root), "forward")
    day = _summary_for(view, "2026-07-16")

    assert day["coverage_complete"] is True
    assert day["coverage_expected"] == 1
    assert day["not_yet_registered_strategies"] == 1
    beta = next(row for row in view["strategy_summaries"] if row["strategy_id"] == "beta")
    assert beta["registry_inception_date"] == "2026-07-17"
    assert beta["registration_status"] == "not_yet_registered"


def test_forward_summaries_exclude_impossible_pre_inception_rows(
    tmp_path: Path,
) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [
            _row(
                date="2026-07-16",
                mode="forward",
                strategy_id="alpha",
                strategy_semantics_fingerprint="alpha-v1",
            ),
            _row(
                date="2026-07-16",
                mode="forward",
                strategy_id="beta",
                strategy_semantics_fingerprint="beta-v1",
                ending_equity="1500",
                total_pnl="500",
                daily_return_pct="0.5",
                cumulative_return_pct="0.5",
            ),
        ],
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("beta", "v1.0", "beta-v1"),
        ],
        strategy_registered_at={
            ("alpha", "v1.0"): "2026-07-01T12:00:00+00:00",
            ("beta", "v1.0"): "2026-07-16T12:00:00+00:00",
        },
        strategy_activation_policy={
            ("beta", "v1.0"): "next_market_session_after_registration"
        },
    )

    view = build_paper_ops_calendar_view(
        load_paper_ops_calendar(fixture.root), "forward"
    )
    day = _summary_for(view, "2026-07-16")
    beta = next(row for row in view["strategy_summaries"] if row["strategy_id"] == "beta")

    assert len(view["impossible_forward_rows"]) == 1
    assert {row["strategy_id"] for row in view["official_rows"]} == {"alpha"}
    assert day["fleet_daily_return"] == Decimal("0.01")
    assert beta["registration_status"] == "not_yet_registered"
    assert beta["session_count"] == 0
    assert beta["period_return"] is None


def test_forward_view_exposes_whole_missing_market_session(tmp_path: Path) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [
            _row(
                date="2026-07-14",
                mode="forward",
                strategy_id="alpha",
                strategy_semantics_fingerprint="alpha-v1",
            ),
            _row(
                date="2026-07-16",
                mode="forward",
                strategy_id="alpha",
                strategy_semantics_fingerprint="alpha-v1",
            ),
        ],
        officials=[_official("alpha", "v1.0", "alpha-v1")],
    )

    view = build_paper_ops_calendar_view(
        load_paper_ops_calendar(fixture.root), "forward"
    )
    missing = _summary_for(view, "2026-07-15")

    assert view["dates"] == ["2026-07-14", "2026-07-15", "2026-07-16"]
    assert missing["coverage_status"] == "missing"
    assert missing["missing_strategies"] == 1
    assert missing["fleet_daily_return"] is None


def test_unknown_activation_policy_fails_closed(tmp_path: Path) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [_row(strategy_semantics_fingerprint="alpha-v1")],
        officials=[_official("alpha", "v1.0", "alpha-v1")],
        strategy_activation_policy={("alpha", "v1.0"): "same_bar_magic"},
    )

    with pytest.raises(PaperOpsCalendarError, match="activation_policy is unsupported"):
        load_paper_ops_calendar(fixture.root)


def test_pre_registration_replay_row_remains_counterfactual_evidence(
    tmp_path: Path,
) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [_row(strategy_semantics_fingerprint="alpha-v1")],
        officials=[_official("alpha", "v1.0", "alpha-v1")],
        strategy_registered_at={
            ("alpha", "v1.0"): "2026-07-15T12:00:00+00:00"
        },
    )

    dataset = load_paper_ops_calendar(fixture.root)
    view = build_paper_ops_calendar_view(dataset, "replay")
    day = _summary_for(view, "2026-07-14")
    row = view["official_rows"][0]

    assert row["registration_status"] == "not_yet_registered"
    assert row["evidence_scope"] == "counterfactual_replay"
    assert day["claim_scope"] == "counterfactual_replay"
    assert day["counterfactual_strategies"] == 1
    assert day["not_yet_registered_strategies"] == 1
    assert day["fleet_daily_return"] == Decimal("0.01")


def test_missing_registry_inception_manifest_fails_closed(tmp_path: Path) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [_row(strategy_semantics_fingerprint="alpha-v1")],
        officials=[_official("alpha", "v1.0", "alpha-v1")],
    )
    (fixture.root / "state" / "strategy_semantics_manifest.json").unlink()

    with pytest.raises(PaperOpsCalendarError, match="semantics manifest is missing"):
        load_paper_ops_calendar(fixture.root)


def test_missing_values_and_incomplete_coverage_remain_unavailable(tmp_path: Path) -> None:
    rows = [
        _row(
            date="2026-07-13",
            strategy_id="alpha",
            strategy_semantics_fingerprint="alpha-v1",
        ),
        _row(
            date="2026-07-13",
            strategy_id="beta",
            strategy_semantics_fingerprint="beta-v1",
        ),
        _row(
            date="2026-07-14",
            strategy_id="alpha",
            strategy_semantics_fingerprint="alpha-v1",
            daily_return_pct=None,
            cumulative_return_pct=None,
            drawdown_pct=None,
        ),
    ]
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        rows,
        officials=[
            _official("alpha", "v1.0", "alpha-v1"),
            _official("beta", "v1.0", "beta-v1"),
        ],
    )

    dataset = load_paper_ops_calendar(fixture.root)
    view = build_paper_ops_calendar_view(dataset, "replay")
    missing_row = next(row for row in view["rows"] if row["date"] == "2026-07-14")
    incomplete = _summary_for(view, "2026-07-14")

    assert missing_row["daily_return_pct"] is None
    assert missing_row["cumulative_return_pct"] is None
    assert missing_row["drawdown_pct"] is None
    assert incomplete["coverage_complete"] is False
    assert incomplete["fleet_daily_return"] is None
    assert incomplete["fleet_cumulative_return"] is None
    assert incomplete["fleet_daily_pnl"] is None
    assert format_return_fraction(incomplete["fleet_daily_return"]) == "N/A"


def test_duplicate_date_and_full_series_identity_is_rejected(tmp_path: Path) -> None:
    duplicate = _row(strategy_semantics_fingerprint="alpha-v1")
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [duplicate, {**duplicate, "total_pnl": "999"}],
        officials=[_official("alpha", "v1.0", "alpha-v1")],
    )

    with pytest.raises(PaperOpsCalendarError, match="Duplicate PaperOps calendar row"):
        load_paper_ops_calendar(fixture.root)


def test_failed_core_truth_gate_blocks_every_return_claim(tmp_path: Path) -> None:
    rows = [
        _row(strategy_semantics_fingerprint="alpha-v1"),
        _row(
            strategy_id="benchmark_buy_hold_equal_weight",
            strategy_status="benchmark",
            strategy_semantics_fingerprint="benchmark-v1",
        ),
        _row(
            strategy_id="cash_no_trade_baseline",
            strategy_status="baseline",
            strategy_semantics_fingerprint="cash-v1",
            total_pnl="0",
            ending_equity="1000",
            daily_return_pct="0",
            cumulative_return_pct="0",
        ),
    ]
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        rows,
        officials=[_official("alpha", "v1.0", "alpha-v1")],
        gate_statuses={"calendar_truth": "failed"},
    )

    dataset = load_paper_ops_calendar(fixture.root)
    view = build_paper_ops_calendar_view(dataset, "replay")
    day = _summary_for(view, "2026-07-14")

    assert dataset["status"] == "blocked"
    assert view["status"] == "blocked"
    assert day["fleet_daily_return"] is None
    assert day["fleet_cumulative_return"] is None
    assert day["benchmark_daily_return"] is None
    assert day["cash_daily_return"] is None
    assert all(summary["period_return"] is None for summary in view["strategy_summaries"])


@pytest.mark.parametrize("stale_gate", ["reconciliation", "source_bar_truth_replay"])
def test_stale_truth_gate_blocks_newer_calendar_evidence(
    tmp_path: Path,
    stale_gate: str,
) -> None:
    fixture = _write_fixture(
        tmp_path / "paper-ops",
        [_row(strategy_semantics_fingerprint="alpha-v1")],
        officials=[_official("alpha", "v1.0", "alpha-v1")],
    )
    old = fixture.calendar_path.stat().st_mtime - 60
    os.utime(fixture.gate_paths[stale_gate], (old, old))

    dataset = load_paper_ops_calendar(fixture.root)
    view = build_paper_ops_calendar_view(dataset, "replay")
    day = _summary_for(view, "2026-07-14")

    assert view["status"] == "blocked"
    assert day["fleet_daily_return"] is None
    assert day["fleet_cumulative_return"] is None
    assert any("stale" in warning.lower() for warning in view["warnings"])
