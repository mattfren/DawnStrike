import csv
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from intraday_scanner.services.luna_core_universe_service import build_core_universe_contract
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.models import PaperRunMode
from intraday_scanner.v2.paper_ops.universe_handoff import (
    UniverseHandoffError,
    build_universe_handoff,
    load_universe_handoff,
)
from tests.test_paperops_universe_handoff import MARKET_DATE, _core_contract, _morning_root


def _rewrite_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _rehash_core(core: dict[str, object]) -> None:
    unhashed = {
        key: value
        for key, value in core.items()
        if key not in {"content_hash_sha256", "content_hash", "contract_id", "universe_id"}
    }
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    core["content_hash_sha256"] = digest
    core["content_hash"] = digest


def _copy_source_summary_into_cycle(root: Path) -> dict[str, object]:
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    cycle_path = root / "alpha_cycle.json"
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    cycle["source_summary"] = {**summary, "code_sha": cycle["code_sha"]}
    _rewrite_json(cycle_path, cycle)
    return summary


def _sync_source_summary_preserving_fleet(root: Path, summary: dict[str, object]) -> None:
    cycle_path = root / "alpha_cycle.json"
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    prior_summary = cycle.get("source_summary") or {}
    cycle["source_summary"] = {
        **summary,
        "code_sha": prior_summary.get("code_sha"),
        "morning_strategy_adapter": prior_summary.get("morning_strategy_adapter"),
    }
    _rewrite_json(cycle_path, cycle)


def test_cross_scan_mover_summary_splice_is_rejected(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    summary = _copy_source_summary_into_cycle(root)
    summary["run_id"] = "different-scan"
    cycle = json.loads((root / "alpha_cycle.json").read_text(encoding="utf-8"))
    cycle["source_summary"] = {**summary, "code_sha": cycle["code_sha"]}
    _rewrite_json(root / "alpha_cycle.json", cycle)

    with pytest.raises(UniverseHandoffError, match="source summary identity"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_cycle_run_contract_and_source_summary_release_sha_must_match(
    tmp_path: Path,
) -> None:
    root = _morning_root(tmp_path)
    cycle_path = root / "alpha_cycle.json"
    contract_path = root / "alpha_run_contract.json"
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    cycle["code_sha"] = "a" * 40
    cycle["source_summary"]["code_sha"] = "a" * 40
    contract["code_sha"] = "b" * 40
    _rewrite_json(cycle_path, cycle)
    _rewrite_json(contract_path, contract)

    with pytest.raises(UniverseHandoffError, match="release SHA claims are inconsistent"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_governed_core_only_recovery_binds_failed_and_recovery_snapshots(tmp_path: Path) -> None:
    root = _morning_root(tmp_path, source_status="failed")
    failed_snapshot = root / "web_collect" / "premarket_snapshot.csv"
    recovery_snapshot = root / "web_collect" / "core_recovery_snapshot.csv"
    with recovery_snapshot.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "market_date", "source"])
        writer.writeheader()
        writer.writerow({"ticker": "AAA", "market_date": MARKET_DATE, "source": "core"})
    standalone = json.loads(
        (root / "web_collect" / "source_summary.json").read_text(encoding="utf-8")
    )
    cycle = json.loads((root / "alpha_cycle.json").read_text(encoding="utf-8"))
    cycle["source_summary"] = {
        **standalone,
        "code_sha": cycle["code_sha"],
        "status": "success",
        "mover_lane_status": "SOURCE_FAILED",
        "failed_mover_snapshot_path": str(failed_snapshot),
        "snapshot_path": str(recovery_snapshot),
    }
    _rewrite_json(root / "alpha_cycle.json", cycle)

    payload = build_universe_handoff(root, MARKET_DATE, allow_test_override=True)

    assert payload["universe_symbols"] == ["AAA"]
    assert payload["mover_source"]["available_count"] == 0
    assert payload["coverage"]["status"] == "PARTIAL"


def test_cycle_core_claim_must_match_sibling_core_contract(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    cycle = json.loads((root / "alpha_cycle.json").read_text(encoding="utf-8"))
    cycle["core_universe"] = _core_contract()
    cycle["core_universe"]["status"] = "DATA_UNAVAILABLE"
    _rewrite_json(root / "alpha_cycle.json", cycle)

    with pytest.raises(UniverseHandoffError, match="cycle core universe claim"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_ready_index_with_source_error_is_fail_closed(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    core_path = root / "core_universe_contract.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["source_artifacts"][0]["error_codes"] = ["hostile_error"]
    _rehash_core(core)
    _rewrite_json(core_path, core)

    with pytest.raises(UniverseHandoffError, match="READY source artifact has errors"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_data_unavailable_cannot_claim_both_ready_index_lanes(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    core_path = root / "core_universe_contract.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["status"] = "DATA_UNAVAILABLE"
    _rehash_core(core)
    _rewrite_json(core_path, core)

    with pytest.raises(UniverseHandoffError, match="both READY index lanes"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_duplicate_and_partial_mover_truth_is_named_and_unique(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    snapshot = root / "web_collect" / "premarket_snapshot.csv"
    with snapshot.open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["BBB", MARKET_DATE, "mover"])
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(status="partial", candidate_count=3, source_failures=1)
    _rewrite_json(summary_path, summary)
    _sync_source_summary_preserving_fleet(root, summary)

    payload = build_universe_handoff(root, MARKET_DATE, allow_test_override=True)

    assert payload["coverage"]["status"] == "PARTIAL"
    assert payload["mover_source"]["available_count"] == 2
    assert "governed_mover_snapshot_duplicate_symbols" in payload["coverage"]["shortfall_reasons"]
    assert "governed_mover_source_partial" in payload["coverage"]["shortfall_reasons"]


def test_mover_declared_count_mismatch_is_named_partial_truth(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["candidate_count"] = 99
    _rewrite_json(summary_path, summary)
    _sync_source_summary_preserving_fleet(root, summary)

    payload = build_universe_handoff(root, MARKET_DATE, allow_test_override=True)

    assert payload["coverage"]["status"] == "PARTIAL"
    assert "governed_mover_snapshot_count_mismatch" in payload["coverage"]["shortfall_reasons"]


def test_blocked_morning_adapter_is_partial_but_current_paperops_nine_remain_runnable(
    tmp_path: Path,
) -> None:
    root = _morning_root(tmp_path)
    summary = _copy_source_summary_into_cycle(root)
    summary["morning_strategy_adapter"] = {
        "status": "BLOCKED_PRIOR_SESSION_EVIDENCE",
        "enabled_strategy_ids": [],
        "rows": [],
    }
    cycle = json.loads((root / "alpha_cycle.json").read_text(encoding="utf-8"))
    cycle["source_summary"] = {**summary, "code_sha": cycle["code_sha"]}
    _rewrite_json(root / "alpha_cycle.json", cycle)

    payload = build_universe_handoff(root, MARKET_DATE, allow_test_override=True)
    handoff_path = root / "paperops_universe_handoff.json"
    build_universe_handoff(root, MARKET_DATE, output_path=handoff_path, allow_test_override=True)

    assert payload["coverage"]["status"] == "PARTIAL"
    assert "morning_strategy_fleet_incomplete" in payload["coverage"]["shortfall_reasons"]
    assert (
        load_universe_handoff(
            handoff_path,
            market_date=MARKET_DATE,
            require_production=False,
        )["strategy_fleet"]["declared_paperops_strategy_ids"]
        == payload["strategy_fleet"]["expected_strategy_ids"]
    )
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    config, handoff = paper_ops_engine._run_config_with_universe_handoff(
        paths,
        run_date=date.fromisoformat(MARKET_DATE),
        mode=PaperRunMode.REPLAY,
        universe_handoff_path=handoff_path,
        scheduled_production=False,
        expected_code_sha="a" * 40,
    )
    strategies = paper_ops_engine._strategies_eligible_for_run(
        paths,
        config=config,
        run_date=date.fromisoformat(MARKET_DATE),
        mode=PaperRunMode.REPLAY,
    )
    assert handoff and handoff["coverage"]["status"] == "PARTIAL"
    assert len(strategies) == 9


def test_core_service_demotes_source_artifact_error_to_data_unavailable() -> None:
    def manifest(index: str, symbol: str, *, bad: bool = False) -> dict[str, object]:
        return {
            "source_id": f"fixture-{symbol}",
            "source_uri": f"https://example.test/{symbol}",
            "index_name": index,
            "expected_count": 1,
            "effective_date": MARKET_DATE,
            "observed_at": f"{MARKET_DATE}T12:00:00+00:00",
            "completeness_verdict": "INCOMPLETE" if bad else "COMPLETE",
            "members": [{"symbol": symbol, "index_memberships": [index]}],
        }

    contract = build_core_universe_contract(
        [manifest("S&P 500", "AAA", bad=True), manifest("Nasdaq-100", "BBB")],
        observed_at=f"{MARKET_DATE}T12:00:00+00:00",
        market_date=MARKET_DATE,
        allow_test_override=True,
    )

    assert contract["status"] == "DATA_UNAVAILABLE"
    assert contract["index_verdicts"]["S&P 500"]["status"] == "DATA_UNAVAILABLE"
    assert contract["index_verdicts"]["Nasdaq-100"]["status"] == "READY"
