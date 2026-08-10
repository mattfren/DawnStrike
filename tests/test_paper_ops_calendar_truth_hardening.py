from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops.calendar_truth import (
    _completed_report_coverage,
    _duplicates,
    _math_mismatches,
    _read_strict_ledger,
    verify_calendar_truth,
)
from intraday_scanner.v2.paper_ops.engine import CALENDAR_FIELDNAMES, PaperOpsPaths, init
from intraday_scanner.v2.paper_ops.observer_safety import PaperOpsObserverBlocked
from intraday_scanner.v2.paper_ops.storage import read_json, write_json, write_jsonl


def _seed_calendar_header(root: Path) -> None:
    (root / "calendar" / "strategy_daily_returns.csv").write_text(
        ",".join(CALENDAR_FIELDNAMES) + "\n", encoding="utf-8"
    )


def _write_calendar_rows(root: Path, rows: list[dict[str, object]]) -> None:
    canonical_rows: list[dict[str, object]] = []
    for overrides in rows:
        row: dict[str, object] = {field: "0" for field in CALENDAR_FIELDNAMES}
        row.update(
            {
                "date": "2026-01-05",
                "mode": "forward",
                "strategy_id": "fixture-strategy",
                "strategy_version": "fixture-v1",
                "strategy_status": "candidate",
                "execution_policy_version": "fixture-policy-v1",
                "strategy_semantics_fingerprint": "unknown",
                "data_snapshot_id": "fixture-snapshot",
                "warnings": "",
                "run_id": "fixture-run",
            }
        )
        row.update(overrides)
        canonical_rows.append(row)
    with (root / "calendar" / "strategy_daily_returns.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CALENDAR_FIELDNAMES)
        writer.writeheader()
        writer.writerows(canonical_rows)


def test_calendar_truth_fails_closed_on_empty_initialized_root(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)

    with pytest.raises(PaperOpsObserverBlocked, match="MISSING_INPUT"):
        verify_calendar_truth(output_root=root)


def test_strict_ledger_rejects_non_objects_blank_and_duplicate_event_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    valid = {
        "event_id": "duplicate",
        "event_type": "paper_no_setup_decision",
        "mode": "forward",
        "payload": {
            "mode": "forward",
            "run_id": "run",
            "strategy_id": "strategy",
            "symbol": "SPY",
        },
        "run_id": "run",
        "schema_version": "v2.paper_ledger_event.v1",
        "strategy_id": "strategy",
        "symbol": "SPY",
        "trade_date": "2026-01-05",
    }
    blank_id = {**valid, "event_id": ""}
    path.write_text(
        "\n".join(
            (
                json.dumps(["silently-droppable-array"]),
                json.dumps(valid),
                json.dumps(valid),
                json.dumps(blank_id),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    rows, mismatches = _read_strict_ledger(path)

    assert len(rows) == 3
    assert "ledger line 1 is not an object" in mismatches
    assert "ledger line 4 has no event_id" in mismatches
    assert "duplicate ledger event_id duplicate" in mismatches


def test_strict_ledger_rejects_schema_and_envelope_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "event",
                "event_type": "paper_fill",
                "mode": "forward",
                "payload": {
                    "mode": "replay",
                    "run_id": "wrong-run",
                    "strategy_id": "wrong-strategy",
                    "symbol": "QQQ",
                },
                "run_id": "run",
                "schema_version": "unsupported",
                "strategy_id": "strategy",
                "symbol": "SPY",
                "trade_date": "2026-01-05",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _, mismatches = _read_strict_ledger(path)

    assert "ledger line 1 has unsupported event schema" in mismatches
    for field in ("mode", "run_id", "strategy_id", "symbol"):
        assert f"ledger line 1 envelope/payload {field} mismatch" in mismatches


def test_strict_ledger_preserves_valid_cross_day_lifecycle_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "pending-check",
                "event_type": "paper_order_pending_no_fill_data",
                "mode": "forward",
                "payload": {
                    "lifecycle_run_id": "run-today",
                    "mode": "forward",
                    "order_id": "order-yesterday",
                    "origin_run_id": "run-yesterday",
                    "run_id": "run-yesterday",
                    "strategy_id": "strategy",
                    "symbol": "SPY",
                    "trade_date": "2026-01-05",
                },
                "run_id": "run-today",
                "schema_version": "v2.paper_ledger_event.v1",
                "strategy_id": "strategy",
                "symbol": "SPY",
                "trade_date": "2026-01-06",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _, mismatches = _read_strict_ledger(path)

    assert mismatches == []


def test_strict_ledger_rejects_conflicting_lifecycle_and_origin_runs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "pending-check",
                "event_type": "paper_order_pending_no_fill_data",
                "mode": "forward",
                "payload": {
                    "lifecycle_run_id": "wrong-lifecycle-run",
                    "mode": "forward",
                    "origin_run_id": "wrong-origin-run",
                    "run_id": "run-yesterday",
                    "strategy_id": "strategy",
                    "symbol": "SPY",
                },
                "run_id": "run-today",
                "schema_version": "v2.paper_ledger_event.v1",
                "strategy_id": "strategy",
                "symbol": "SPY",
                "trade_date": "2026-01-06",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _, mismatches = _read_strict_ledger(path)

    assert "ledger line 1 envelope/payload lifecycle_run_id mismatch" in mismatches
    assert "ledger line 1 payload origin/run_id mismatch" in mismatches


def test_calendar_truth_blocks_pending_transaction_before_reading(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    registry = read_json(root / "state" / "strategy_registry.json", [])
    assert isinstance(registry, list) and registry
    strategy = registry[0]
    assert isinstance(strategy, dict)
    order = {
        "execution_policy_version": strategy["execution_policy_version"],
        "mode": "forward",
        "order_id": "pending-journal-order",
        "strategy_id": strategy["strategy_id"],
        "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
        "strategy_version": strategy["strategy_version"],
        "symbol": "SPY",
    }
    event = {
        "event_id": "pending-journal-event",
        "event_type": "paper_order_created",
        "mode": "forward",
        "payload": order,
        "run_id": "paper_ops:forward:2026-01-05:snapshot",
        "strategy_id": strategy["strategy_id"],
        "symbol": "SPY",
        "trade_date": "2026-01-05",
    }
    state_updates = {"state/pending_orders.json": [order]}
    checksum_payload = {"events": [event], "state_updates": state_updates}
    journal = {
        **checksum_payload,
        "schema_version": "v2.paper_transaction.v1",
        "transaction_id": hashlib.sha256(
            json.dumps(
                checksum_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    journal_path = root / "state" / "paper_transaction_pending.json"
    write_json(journal_path, journal)
    (root / "state" / ".paper_transaction.lock").unlink(missing_ok=True)

    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    with pytest.raises(PaperOpsObserverBlocked, match="BLOCKED_PENDING_RECOVERY"):
        verify_calendar_truth(output_root=root)
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before
    assert journal_path.exists()
    assert not (root / "state" / ".paper_transaction.lock").exists()


def test_calendar_truth_never_inherits_current_lineage_for_legacy_event(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_calendar_header(root)
    registry = read_json(root / "state" / "strategy_registry.json", [])
    assert isinstance(registry, list) and registry
    strategy = registry[0]
    assert isinstance(strategy, dict)
    strategy_id = str(strategy["strategy_id"])
    _write_calendar_rows(
        root,
        [
            {
                "strategy_id": "preflight-evidence",
                "run_id": "legacy-run",
            }
        ],
    )
    write_jsonl(
        root / "ledger" / "paper_ledger.jsonl",
        [
            {
                "event_id": "legacy-decision",
                "event_type": "paper_pick_decision",
                "mode": "forward",
                "payload": {
                    "mode": "forward",
                    "pick_id": "legacy-pick",
                    "strategy_id": strategy_id,
                    "symbol": "SPY",
                },
                "run_id": "legacy-run",
                "strategy_id": strategy_id,
                "symbol": "SPY",
                "trade_date": "2026-01-05",
            }
        ],
    )

    result = verify_calendar_truth(output_root=root)

    assert result.status == "failed"
    assert (
        f"2026-01-05:forward:{strategy_id}:unknown:legacy_unspecified:unknown"
        in result.missing_rows
    )
    assert not any(
        str(strategy["execution_policy_version"]) in row
        for row in result.missing_rows
        if row.startswith(f"2026-01-05:forward:{strategy_id}:")
    )


def test_calendar_truth_blocks_completed_report_without_exact_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_calendar_header(root)
    run_id = "paper_ops:forward:2026-01-05:snapshot"
    write_json(
        root / "reports" / "daily" / "forward_2026-01-05.json",
        {
            "data_snapshot_id": "snapshot",
            "date": "2026-01-05",
            "mode": "forward",
            "run_id": run_id,
            "stats": {"phase": "close"},
        },
    )

    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(PaperOpsObserverBlocked, match="MISSING_INPUT"):
        verify_calendar_truth(output_root=root)
    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert run_id


def test_calendar_truth_detects_strategy_missing_from_ledger_and_calendar(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    registry = read_json(root / "state" / "strategy_registry.json", [])
    assert isinstance(registry, list) and len(registry) > 1
    registered = [row for row in registry if isinstance(row, dict)]
    omitted = registered[-1]
    included = registered[:-1]
    semantics_manifest = read_json(
        root / "state" / "strategy_semantics_manifest.json",
        {},
    )
    semantics_rows = semantics_manifest.get("strategies", {})
    run_date = max(
        str(row["coverage_inception_date"])
        for row in semantics_rows.values()
        if isinstance(row, dict)
    )
    run_id = f"paper_ops:forward:{run_date}:snapshot"
    rows = [
        {
            "date": run_date,
            "mode": "forward",
            "strategy_id": strategy["strategy_id"],
            "strategy_version": strategy["strategy_version"],
            "execution_policy_version": strategy["execution_policy_version"],
            "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
            "run_id": run_id,
            "starting_equity": "100000",
            "ending_equity": "100000",
            "realized_pnl": "0",
            "unrealized_pnl": "0",
            "total_pnl": "0",
            "daily_return_pct": "0",
            "cumulative_return_pct": "0",
            "trades_opened": "0",
            "trades_closed": "0",
            "pending_orders": "0",
        }
        for strategy in included
    ]
    _write_calendar_rows(root, rows)
    write_jsonl(
        root / "ledger" / "paper_ledger.jsonl",
        [
            {
                "event_id": f"decision:{strategy['strategy_id']}",
                "event_type": "paper_no_setup_decision",
                "mode": "forward",
                "payload": {
                    "execution_policy_version": strategy["execution_policy_version"],
                    "mode": "forward",
                    "strategy_id": strategy["strategy_id"],
                    "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
                    "strategy_version": strategy["strategy_version"],
                    "symbol": "SPY",
                },
                "run_id": run_id,
                "strategy_id": strategy["strategy_id"],
                "symbol": "SPY",
                "trade_date": run_date,
            }
            for strategy in included
        ],
    )
    write_json(
        root / "reports" / "daily" / f"forward_{run_date}.json",
        {
            "date": run_date,
            "mode": "forward",
            "run_id": run_id,
            "stats": {"phase": "close"},
        },
    )

    result = verify_calendar_truth(output_root=root)

    omitted_series = ":".join(
        str(omitted[field])
        for field in (
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
        )
    )
    label = f"{run_date}:forward:{run_id}:{omitted_series}"
    assert f"{run_date}:forward:{omitted_series}" in result.missing_rows
    assert f"completed report has no exact ledger strategy {label}" in result.missing_rows
    assert f"completed report has no exact calendar strategy {label}" in result.missing_rows


def test_calendar_identity_never_blends_semantics_fingerprints() -> None:
    shared = {
        "mode": "forward",
        "strategy_id": "same_strategy",
        "strategy_version": "v1",
        "execution_policy_version": "policy-v1",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "drawdown_pct": "0",
        "trades_opened": "0",
        "trades_closed": "0",
        "pending_orders": "0",
        "open_positions": "0",
        "wins": "0",
        "losses": "0",
        "flats": "0",
        "average_r": "0",
        "expectancy_r": "0",
        "exposure_pct": "0",
        "fees_paid": "0",
        "slippage_estimate": "0",
    }
    rows = [
        {
            **shared,
            "date": "2026-01-05",
            "strategy_semantics_fingerprint": "a" * 64,
            "starting_equity": "100",
            "ending_equity": "100",
            "total_pnl": "0",
            "daily_return_pct": "0",
            "cumulative_return_pct": "0",
        },
        {
            **shared,
            "date": "2026-01-05",
            "strategy_semantics_fingerprint": "b" * 64,
            "starting_equity": "200",
            "ending_equity": "200",
            "total_pnl": "0",
            "daily_return_pct": "0",
            "cumulative_return_pct": "0",
        },
        {
            **shared,
            "date": "2026-01-06",
            "strategy_semantics_fingerprint": "a" * 64,
            "starting_equity": "100",
            "ending_equity": "110",
            "total_pnl": "10",
            "daily_return_pct": "0.1",
            "cumulative_return_pct": "0.1",
        },
        {
            **shared,
            "date": "2026-01-06",
            "strategy_semantics_fingerprint": "b" * 64,
            "starting_equity": "200",
            "ending_equity": "180",
            "total_pnl": "-20",
            "daily_return_pct": "-0.1",
            "cumulative_return_pct": "-0.1",
        },
    ]

    assert _duplicates(rows) == []
    assert _math_mismatches(rows) == []


def test_calendar_math_rejects_missing_invalid_and_non_finite_values() -> None:
    base = {
        "date": "2026-01-05",
        "mode": "forward",
        "strategy_id": "strategy",
        "strategy_version": "v1",
        "execution_policy_version": "policy-v1",
        "strategy_semantics_fingerprint": "a" * 64,
        "starting_equity": "100",
        "ending_equity": "100",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "total_pnl": "0",
        "daily_return_pct": "0",
        "cumulative_return_pct": "0",
        "drawdown_pct": "0",
        "trades_opened": "0",
        "trades_closed": "0",
        "pending_orders": "0",
        "open_positions": "0",
        "wins": "0",
        "losses": "0",
        "flats": "0",
        "average_r": "0",
        "expectancy_r": "0",
        "exposure_pct": "0",
        "fees_paid": "0",
        "slippage_estimate": "0",
    }
    rows = [
        {**base, "daily_return_pct": "NaN"},
        {**base, "date": "2026-01-06", "ending_equity": "Infinity"},
        {**base, "date": "2026-01-07", "realized_pnl": "not-a-number"},
        {**base, "date": "2026-01-08", "fees_paid": ""},
    ]

    mismatches = _math_mismatches(rows)

    assert any("daily_return_pct is missing, invalid, or non-finite" in row for row in mismatches)
    assert any("ending_equity is missing, invalid, or non-finite" in row for row in mismatches)
    assert any("realized_pnl is missing, invalid, or non-finite" in row for row in mismatches)
    assert any("fees_paid is missing, invalid, or non-finite" in row for row in mismatches)


def test_calendar_math_rejects_impossible_counts_and_costs() -> None:
    row = {
        "date": "2026-01-05",
        "mode": "forward",
        "strategy_id": "strategy",
        "strategy_version": "v1",
        "execution_policy_version": "policy-v1",
        "strategy_semantics_fingerprint": "a" * 64,
        "starting_equity": "100",
        "ending_equity": "100",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "total_pnl": "0",
        "daily_return_pct": "0",
        "cumulative_return_pct": "0",
        "drawdown_pct": "0.1",
        "trades_opened": "0",
        "trades_closed": "1",
        "pending_orders": "0",
        "open_positions": "0",
        "wins": "0",
        "losses": "0",
        "flats": "0",
        "average_r": "0",
        "expectancy_r": "0",
        "exposure_pct": "-1",
        "fees_paid": "-1",
        "slippage_estimate": "-1",
    }

    mismatches = _math_mismatches([row])

    assert any("close outcome counts do not equal trades closed" in item for item in mismatches)
    assert any("fees_paid must not be negative" in item for item in mismatches)
    assert any("slippage_estimate must not be negative" in item for item in mismatches)
    assert any("exposure_pct must not be negative" in item for item in mismatches)
    assert any("drawdown must not be positive" in item for item in mismatches)


def test_completed_report_requires_current_fingerprint_ledger_coverage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    registry = read_json(root / "state" / "strategy_registry.json", [])
    assert isinstance(registry, list) and registry
    registered = [row for row in registry if isinstance(row, dict)]
    run_date = "2026-01-05"
    run_id = f"paper_ops:forward:{run_date}:snapshot"
    stale_fingerprint = "0" * 64
    rows = [
        {
            "date": run_date,
            "mode": "forward",
            "run_id": run_id,
            "strategy_id": strategy["strategy_id"],
            "strategy_version": strategy["strategy_version"],
            "execution_policy_version": strategy["execution_policy_version"],
            "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
        }
        for strategy in registered
    ]
    stale_row = {
        **rows[0],
        "strategy_semantics_fingerprint": stale_fingerprint,
    }
    rows.append(stale_row)
    events = []
    for index, strategy in enumerate(registered):
        fingerprint = (
            stale_fingerprint if index == 0 else strategy["strategy_semantics_fingerprint"]
        )
        events.append(
            {
                "event_id": f"decision:{strategy['strategy_id']}",
                "event_type": "paper_no_setup_decision",
                "mode": "forward",
                "payload": {
                    "strategy_id": strategy["strategy_id"],
                    "strategy_version": strategy["strategy_version"],
                    "execution_policy_version": strategy["execution_policy_version"],
                    "strategy_semantics_fingerprint": fingerprint,
                },
                "run_id": run_id,
                "strategy_id": strategy["strategy_id"],
                "trade_date": run_date,
            }
        )
    write_json(
        root / "reports" / "daily" / f"forward_{run_date}.json",
        {
            "date": run_date,
            "mode": "forward",
            "run_id": run_id,
            "stats": {"phase": "close"},
        },
    )

    gaps = _completed_report_coverage(paths, rows, events)

    current = registered[0]
    current_series = ":".join(
        str(current[field])
        for field in (
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
        )
    )
    assert (
        f"completed report has no exact ledger strategy "
        f"{run_date}:forward:{run_id}:{current_series}"
    ) in gaps


def test_completed_report_uses_that_days_series_after_registry_rollover(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    registry = read_json(root / "state" / "strategy_registry.json", [])
    assert isinstance(registry, list) and registry
    registered = [row for row in registry if isinstance(row, dict)]
    run_date = "2026-01-05"
    run_id = f"paper_ops:forward:{run_date}:snapshot"
    historical_series = [
        {
            "strategy_id": strategy["strategy_id"],
            "strategy_version": "historical-v1",
            "execution_policy_version": "historical-policy-v1",
            "strategy_semantics_fingerprint": "a" * 64,
        }
        for strategy in registered
    ]
    decisions = [
        {
            **series,
            "market_date": run_date,
            "mode": "forward",
            "run_id": run_id,
            "symbol": "SPY",
        }
        for series in historical_series
    ]
    events = [
        {
            "event_id": f"decision:{series['strategy_id']}",
            "event_type": "paper_no_setup_decision",
            "mode": "forward",
            "payload": dict(series),
            "run_id": run_id,
            "strategy_id": series["strategy_id"],
            "trade_date": run_date,
        }
        for series in historical_series
    ]
    rows = [
        {
            "date": run_date,
            "mode": "forward",
            "run_id": run_id,
            **series,
        }
        for series in historical_series
    ]
    write_json(
        root / "reports" / "daily" / f"forward_{run_date}.json",
        {
            "date": run_date,
            "mode": "forward",
            "run_id": run_id,
            "stats": {"phase": "close"},
        },
    )
    write_json(
        root / "exports" / f"strategy_decisions_forward_{run_date}.json",
        decisions,
    )

    gaps = _completed_report_coverage(paths, rows, events)

    assert gaps == []


def test_completed_report_with_incomplete_identity_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    write_json(
        root / "reports" / "daily" / "forward_2026-01-05.json",
        {
            "date": "2026-01-05",
            "mode": "forward",
            "phases": {"close": {"stats": {"phase": "close"}}},
        },
    )

    gaps = _completed_report_coverage(paths, [], [])

    assert any(
        item.startswith("completed report identity is incomplete ") and "run_id=<missing>" in item
        for item in gaps
    )
