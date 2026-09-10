from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.observation.cohort import (
    CohortError,
    _run_bounded,
    _session_status,
    expected_market_sessions,
    prepare_cohort,
    resume_cohort,
    validate_fresh_scope,
)


def _scope(tmp_path: Path, market_date: str = "2026-09-10", mover_count: int = 181) -> Path:
    source = tmp_path / "source.csv"
    source.write_text("ticker\nAAA\n", encoding="utf-8")
    movers = [
        {
            "symbol": f"M{index:03d}",
            "membership": "selected" if index < 2 else "rejected" if index < 12 else "unselected",
            "source_lane": "mover",
            "as_of": market_date,
        }
        for index in range(mover_count)
    ]
    panel = [
        {
            "symbol": symbol,
            "membership": "selected",
            "source_lane": "reference_panel",
            "as_of": market_date,
        }
        for symbol in ("DIA", "IWM", "QQQ", "SPY", "TLT")
    ]
    value = {
        "schema_version": "dawnstrike.observation.scope_declaration.v1",
        "market_date": market_date,
        "scope_policy": {
            "core_index_membership": "separate unavailable scope; never inferred here"
        },
        "producer_completeness": {
            "status": "COMPLETE",
            "source_count": mover_count,
            "declared_count": mover_count,
            "included_count": mover_count,
            "source_as_of": market_date,
            "truncated": False,
            "survivorship_filter": False,
        },
        "source_identity": {"market_date": market_date, "source_kind": "fresh_daily_mover_census"},
        "source_artifacts": {
            "snapshot": {
                "path": str(source),
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        },
        "scopes": {"original_small_cap_gap": movers, "liquid_reference_panel": panel},
    }
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_expected_sessions_are_finite_and_retain_early_close_identity() -> None:
    sessions = expected_market_sessions("2026-11-20")
    assert len(sessions) == 10
    early = next(row for row in sessions if row["market_date"] == "2026-11-27")
    assert early["is_early_close"] is True
    assert early["session_status"] == "early_close"
    assert early["exchange_session_id"] == "XNYS:2026-11-27:regular"


def test_scope_requires_fresh_exact_date_and_full_mover_census(tmp_path: Path) -> None:
    path = _scope(tmp_path)
    identity = validate_fresh_scope(path, expected_date="2026-09-10")
    assert identity["mover_count"] == 181
    assert identity["reference_count"] == 5
    assert identity["sampling"]["sampled_count"] == 12
    with pytest.raises(CohortError, match="market_date"):
        validate_fresh_scope(path, expected_date="2026-09-11")


@pytest.mark.parametrize("mover_count", [180, 182])
def test_scope_accepts_complete_variable_date_bound_cardinality(
    tmp_path: Path, mover_count: int
) -> None:
    path = _scope(tmp_path, mover_count=mover_count)
    identity = validate_fresh_scope(path, expected_date="2026-09-10")
    assert identity["mover_count"] == mover_count


def test_stale_historical_scope_proof_is_rejected(tmp_path: Path) -> None:
    path = _scope(tmp_path, market_date="2026-09-09")
    with pytest.raises(CohortError, match="market_date"):
        validate_fresh_scope(path, expected_date="2026-09-10")


def test_scope_source_mutation_and_missing_source_fail_closed(tmp_path: Path) -> None:
    path = _scope(tmp_path)
    source = tmp_path / "source.csv"
    source.write_text("ticker\nCHANGED\n", encoding="utf-8")
    with pytest.raises(CohortError, match="missing or changed"):
        validate_fresh_scope(path, expected_date="2026-09-10")
    source.unlink()
    with pytest.raises(CohortError, match="missing or changed"):
        validate_fresh_scope(path, expected_date="2026-09-10")


def test_prepare_and_resume_missing_source_are_durable_and_do_not_renew(tmp_path: Path) -> None:
    output = tmp_path / "cohort"
    plan = prepare_cohort(
        output_root=output,
        input_root=tmp_path / "inputs",
        scope_root=tmp_path / "scopes",
        repo_root=Path(r"C:\r\dawnstrike-remediation-candidate-20260909"),
    )
    assert plan["expected_session_count"] == 10
    assert plan["safety"]["no_auto_renewal"] is True
    before_close = resume_cohort(
        output_root=output,
        input_root=tmp_path / "inputs",
        repo_root=Path(r"C:\r\dawnstrike-remediation-candidate-20260909"),
        now=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
    )
    assert {row["status"] for row in before_close["sessions"]} == {"EXPECTED"}
    after_close = resume_cohort(
        output_root=output,
        input_root=tmp_path / "inputs",
        repo_root=Path(r"C:\r\dawnstrike-remediation-candidate-20260909"),
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert all(row["status"] == "MISSED_SESSION" for row in after_close["sessions"])
    assert after_close["safety"]["no_auto_renewal"] is True


def test_scope_rejects_historical_core_substitution(tmp_path: Path) -> None:
    path = _scope(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["scope_policy"]["core_index_membership"] = "518 index members"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CohortError, match="separate core membership"):
        validate_fresh_scope(path, expected_date="2026-09-10")


def test_stop_marker_is_durable_and_coverage_truth_is_explicit(tmp_path: Path) -> None:
    output = tmp_path / "cohort"
    prepare_cohort(
        output_root=output,
        input_root=tmp_path / "inputs",
        scope_root=tmp_path / "scopes",
        repo_root=Path(r"C:\r\dawnstrike-remediation-candidate-20260909"),
    )
    (output / ".cohort.stop").write_text("stop\n", encoding="utf-8")
    stopped = resume_cohort(
        output_root=output,
        input_root=tmp_path / "inputs",
        repo_root=Path(r"C:\r\dawnstrike-remediation-candidate-20260909"),
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert stopped["status"] == "STOPPED"
    assert stopped["sessions"][0]["status"] == "STOPPED"
    complete, reason, coverage = _session_status(
        capture={"status": "COMPLETE", "symbols": ["DIA", "IWM", "QQQ", "SPY", "TLT"]},
        observer={"status": "PARTIAL", "delayed_event_count": 2},
        expected_symbol_count=186,
        corporate_actions_proven=False,
    )
    assert (complete, reason, coverage) == (
        "DEGRADED",
        "non_timely_or_partial_source",
        "REFERENCE_PANEL_ONLY_DELAYED",
    )


def test_bounded_process_records_pid_walltime_and_sampled_tree_memory(tmp_path: Path) -> None:
    stop_path = tmp_path / "stop"
    result = _run_bounded(
        [sys.executable, "-c", "print('observer-only')"],
        cwd=Path.cwd(),
        timeout_seconds=10,
        max_rss_bytes=256 * 1024 * 1024,
        stop_path=stop_path,
    )
    assert result["exit_code"] == 0
    assert result["pid"] > 0
    assert result["pid_start_identity"].startswith(f"{result['pid']}:")
    assert result["wall_seconds"] >= 0
    assert result["resource_peak_is_sampled"] is True
