import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.services.luna_core_universe_service import (
    _canonical_member_hash,
    build_core_universe_contract,
)
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.models import PaperRunMode
from intraday_scanner.v2.paper_ops.universe_handoff import (
    UniverseHandoffError,
    _iso_date,
    _validate_core_contract,
    _validate_runtime_release_sha,
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


def _rehash_handoff(handoff: dict[str, object]) -> None:
    unhashed = {
        key: value
        for key, value in handoff.items()
        if key not in {"content_hash_sha256", "content_hash", "handoff_id", "universe_id"}
    }
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    handoff["content_hash_sha256"] = digest
    handoff["content_hash"] = digest
    handoff["handoff_id"] = "paperops-universe-" + digest[:24]
    handoff["universe_id"] = "paperops-pit-universe-" + digest[:24]


def _attempts_with_one_failure() -> list[dict[str, object]]:
    return [
        {
            "source": "local_inbox",
            "status": "empty",
            "failure_reason": "local inbox is empty",
        },
        {"source": "fixture_success", "status": "success", "failure_reason": ""},
        {
            "source": "fixture_failure",
            "status": "failed",
            "failure_reason": "fixture provider failed",
        },
    ]


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


def test_missing_core_manifest_is_validated_as_lane_local_unavailable() -> None:
    core = build_core_universe_contract(
        None,
        observed_at=datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc),
        market_date=MARKET_DATE,
    )

    assert core["status"] == "DATA_UNAVAILABLE"
    assert core["observed_at"] is None
    assert core["membership_count"] == 0
    assert _validate_core_contract(core, MARKET_DATE) == set()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda core: core.update(status="DATA_UNAVAILABLE", observed_at=None),
        lambda core: core.update(
            status="DATA_UNAVAILABLE",
            observed_at=None,
            index_verdicts={
                **dict(core["index_verdicts"]),
                "S&P 500": {"status": "READY"},
            },
        ),
    ],
    ids=["members-without-observation", "ready-index-without-observation"],
)
def test_data_unavailable_mixed_core_rejects_null_observation(mutation) -> None:
    core = _core_contract()
    mutation(core)
    _rehash_core(core)

    with pytest.raises(UniverseHandoffError, match="requires a fresh observation"):
        _validate_core_contract(core, MARKET_DATE)


@pytest.mark.parametrize(
    "observed_at",
    ["2026-07-01T12:00:00+00:00", "2026-08-29T00:00:00+00:00"],
    ids=["stale", "future"],
)
def test_data_unavailable_mixed_core_rejects_stale_or_future_observation(
    observed_at: str,
) -> None:
    core = _core_contract()
    core.update(status="DATA_UNAVAILABLE", observed_at=observed_at)
    _rehash_core(core)

    with pytest.raises(UniverseHandoffError, match="not fresh"):
        _validate_core_contract(core, MARKET_DATE)


def test_data_unavailable_partial_core_rejects_member_outside_market_date() -> None:
    core = _core_contract()
    future = "2099-01-01"
    core.update(status="DATA_UNAVAILABLE", completeness_verdict="INCOMPLETE")
    core["members"][0]["valid_from"] = future
    core["index_verdicts"]["Nasdaq-100"]["status"] = "DATA_UNAVAILABLE"

    all_records: list[dict[str, object]] = []
    for artifact in core["source_artifacts"]:
        index = artifact["source_binding"]["index"]
        index_records = [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": index,
                "valid_from": future,
                "valid_to": None,
            }
        ]
        member_hash = _canonical_member_hash(index_records)
        artifact["canonical_member_set_hash_sha256"] = member_hash
        artifact["source_binding"]["derived_member_set_hash_sha256"] = member_hash
        all_records.extend(index_records)
    core["canonical_member_set_hash_sha256"] = _canonical_member_hash(all_records)
    _rehash_core(core)

    with pytest.raises(UniverseHandoffError, match="member is not valid"):
        _validate_core_contract(core, MARKET_DATE, allow_test_override=True)


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-28",
        "2026-08-28garbage",
        "garbage2026-08-28",
        "2026-08-2",
        "2026-08-28T13:00",
        "2026-08-28T13:00:00",
        "2026-02-30",
    ],
)
def test_iso_date_rejects_malformed_dates_and_timestamps(value: str) -> None:
    assert _iso_date(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-28T13:00:00Z",
        "2026-08-28T13:00:00+00:00",
        "2026-08-28T08:00:00-05:00",
        "2026-08-28T13:00:00.1234567Z",
        "2026-08-28T23:59:59-12:00",
    ],
)
def test_iso_date_accepts_canonical_date_and_timezone_timestamps(value: str) -> None:
    expected = "2026-08-29" if "23:59:59-12:00" in value else MARKET_DATE
    assert _iso_date(value) == expected


@pytest.mark.parametrize(
    "status",
    ["READY", "DATA_UNAVAILABLE"],
)
@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-08-28garbage",
        "garbage2026-08-28",
        "2026-08-2",
        "2026-08-28T13:00",
        "2026-08-28T13:00:00",
        "2026-02-30",
    ],
)
def test_core_contract_rejects_malformed_non_null_observation(
    status: str, observed_at: str
) -> None:
    if status == "READY":
        core = _core_contract()
    else:
        core = build_core_universe_contract(
            None,
            observed_at=datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc),
            market_date=MARKET_DATE,
        )
    core["status"] = status
    core["observed_at"] = observed_at
    _rehash_core(core)

    with pytest.raises(UniverseHandoffError, match="observation is invalid"):
        _validate_core_contract(core, MARKET_DATE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("research_only", False),
        ("broker_execution", "enabled"),
        ("missing_truth_is_zero", True),
    ],
)
def test_core_safety_claims_are_verified_end_to_end(
    tmp_path: Path, field: str, value: object
) -> None:
    root = _morning_root(tmp_path)
    core_path = root / "core_universe_contract.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core[field] = value
    _rehash_core(core)
    _rewrite_json(core_path, core)

    with pytest.raises(UniverseHandoffError, match="core universe safety binding"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_production_builder_rejects_cycle_sha_not_matching_executing_runtime(
    tmp_path: Path,
) -> None:
    root = _morning_root(tmp_path)

    with pytest.raises(
        UniverseHandoffError, match="does not match executing runtime HEAD"
    ):
        build_universe_handoff(root, MARKET_DATE)


def test_production_builder_rejects_dirty_tracked_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        if args[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="a" * 40)
        return SimpleNamespace(stdout=" M tracked.py\n")

    monkeypatch.setattr("intraday_scanner.v2.paper_ops.universe_handoff.subprocess.run", fake_run)

    with pytest.raises(UniverseHandoffError, match="worktree is dirty"):
        _validate_runtime_release_sha({"code_sha": "a" * 40}, allow_test_override=False)


def test_production_builder_rejects_nonignored_untracked_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        if args[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="a" * 40)
        assert "--untracked-files=all" in args
        return SimpleNamespace(stdout="?? hashlib.py\n")

    monkeypatch.setattr("intraday_scanner.v2.paper_ops.universe_handoff.subprocess.run", fake_run)

    with pytest.raises(UniverseHandoffError, match="worktree is dirty"):
        _validate_runtime_release_sha({"code_sha": "a" * 40}, allow_test_override=False)


def test_production_builder_rejects_runtime_checkout_switch_during_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rev_parse_calls = 0

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        nonlocal rev_parse_calls
        if args[-2:] == ["rev-parse", "HEAD"]:
            rev_parse_calls += 1
            return SimpleNamespace(stdout=("a" if rev_parse_calls == 1 else "b") * 40)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("intraday_scanner.v2.paper_ops.universe_handoff.subprocess.run", fake_run)

    with pytest.raises(UniverseHandoffError, match="HEAD changed during verification"):
        _validate_runtime_release_sha({"code_sha": "a" * 40}, allow_test_override=False)
    assert rev_parse_calls == 2


@pytest.mark.parametrize("generated_at", ["2026-08-28garbage", "2026-08-28T13:00:00"])
def test_cycle_generated_at_rejects_malformed_non_null_timestamp(
    tmp_path: Path, generated_at: str
) -> None:
    root = _morning_root(tmp_path)
    cycle_path = root / "alpha_cycle.json"
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    cycle["generated_at"] = generated_at
    _rewrite_json(cycle_path, cycle)

    with pytest.raises(UniverseHandoffError, match="cycle artifact is stale or cross-date"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


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


@pytest.mark.parametrize("forged_count", [99, True, 1.0])
def test_core_membership_count_must_equal_exact_unique_members(
    tmp_path: Path, forged_count: object
) -> None:
    root = _morning_root(tmp_path)
    core_path = root / "core_universe_contract.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["membership_count"] = forged_count
    _rehash_core(core)
    _rewrite_json(core_path, core)

    with pytest.raises(UniverseHandoffError, match=r"membership[_ ]count(?: binding)?"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_failed_mover_attempt_cannot_be_resigned_as_zero_source_failures(
    tmp_path: Path,
) -> None:
    root = _morning_root(tmp_path)
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        attempts=_attempts_with_one_failure(),
        sources_attempted=3,
        sources_succeeded=1,
        source_failures=0,
    )
    _rewrite_json(summary_path, summary)
    _sync_source_summary_preserving_fleet(root, summary)

    with pytest.raises(UniverseHandoffError, match="attempt counts conflict"):
        build_universe_handoff(root, MARKET_DATE, allow_test_override=True)


def test_genuine_mover_attempt_failure_remains_named_partial(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        attempts=_attempts_with_one_failure(),
        sources_attempted=3,
        sources_succeeded=1,
        source_failures=1,
    )
    _rewrite_json(summary_path, summary)
    _sync_source_summary_preserving_fleet(root, summary)

    payload = build_universe_handoff(root, MARKET_DATE, allow_test_override=True)

    assert payload["coverage"]["status"] == "PARTIAL"
    assert "provider_failures_present" in payload["coverage"]["shortfall_reasons"]


def test_rehashed_handoff_union_counts_must_match_exact_members(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    handoff_path = root / "paperops_universe_handoff.json"
    build_universe_handoff(
        root,
        MARKET_DATE,
        output_path=handoff_path,
        allow_test_override=True,
    )
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["coverage"]["union_count"] = 99
    _rehash_handoff(handoff)
    _rewrite_json(handoff_path, handoff)

    with pytest.raises(UniverseHandoffError, match="coverage union_count binding"):
        load_universe_handoff(
            handoff_path,
            market_date=MARKET_DATE,
            require_production=False,
            verify_sources=False,
        )


def test_duplicate_and_partial_mover_truth_is_named_and_unique(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    snapshot = root / "web_collect" / "premarket_snapshot.csv"
    with snapshot.open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["BBB", MARKET_DATE, "mover"])
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        status="partial",
        candidate_count=3,
        attempts=_attempts_with_one_failure(),
        sources_attempted=3,
        sources_succeeded=1,
        source_failures=1,
    )
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
